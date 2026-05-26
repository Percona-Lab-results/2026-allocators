#!/bin/bash
set -euo pipefail

# HammerDB TPC-C Benchmark Script for MySQL/Percona Server
# Usage: ./run_hammerdb_benchmark.sh <server_binary_path> <thp_enabled> <allocator>
# Example: ./run_hammerdb_benchmark.sh /opt/percona-server/bin/mysqld yes jemalloc53

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="${SCRIPT_DIR}/results"
SERVER_DATA_DIR="${HOME}/servers/data"
MY_CNF="${SCRIPT_DIR}/my.cnf"
HAMMERDB_LOAD_TCL="${SCRIPT_DIR}/hammerdb_load.tcl"
BENCHMARK_DURATION_HOURS=20
VIRTUAL_USERS=80

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# Check command line arguments
if [ $# -lt 3 ]; then
    log_error "Usage: $0 <server_binary_path> <thp_enabled:yes|no> <allocator:jemalloc36|jemalloc53|tcmalloc|glibc>"
    exit 1
fi

SERVER_BINARY="$1"
THP_ENABLED="$2"
ALLOCATOR="$3"

# Validate inputs
if [ ! -f "${SERVER_BINARY}" ]; then
    log_error "Server binary not found: ${SERVER_BINARY}"
    exit 1
fi

if [[ ! "${THP_ENABLED}" =~ ^(yes|no)$ ]]; then
    log_error "THP parameter must be 'yes' or 'no', got: ${THP_ENABLED}"
    exit 1
fi

if [[ ! "${ALLOCATOR}" =~ ^(jemalloc36|jemalloc53|tcmalloc|glibc)$ ]]; then
    log_error "Allocator must be one of: jemalloc36, jemalloc53, tcmalloc, glibc"
    exit 1
fi

# 1. Check if HammerDB 5.0 is installed
log_info "Checking HammerDB 5.0 installation..."
HAMMERDB_CLI=""

# First check current directory
if [ -f "${SCRIPT_DIR}/HammerDB-5.0/hammerdbcli" ]; then
    HAMMERDB_CLI="${SCRIPT_DIR}/HammerDB-5.0/hammerdbcli"
    log_info "Found HammerDB in current directory: ${HAMMERDB_CLI}"
else
    # Not in current directory, try to download
    log_info "HammerDB not found in current directory, downloading..."

    HAMMERDB_URL="https://github.com/TPC-Council/HammerDB/releases/download/v5.0/HammerDB-5.0-Prod-Lin-UBU24.tar.gz"
    HAMMERDB_TARBALL="${SCRIPT_DIR}/HammerDB-5.0-Prod-Lin-UBU24.tar.gz"

    # Download HammerDB
    if ! command -v wget &> /dev/null && ! command -v curl &> /dev/null; then
        log_error "Neither wget nor curl found. Please install one of them to download HammerDB."
        exit 1
    fi

    if command -v wget &> /dev/null; then
        log_info "Downloading with wget..."
        wget -O "${HAMMERDB_TARBALL}" "${HAMMERDB_URL}" || {
            log_error "Failed to download HammerDB from ${HAMMERDB_URL}"
            exit 1
        }
    else
        log_info "Downloading with curl..."
        curl -L -o "${HAMMERDB_TARBALL}" "${HAMMERDB_URL}" || {
            log_error "Failed to download HammerDB from ${HAMMERDB_URL}"
            exit 1
        }
    fi

    # Extract HammerDB
    log_info "Extracting HammerDB to ${SCRIPT_DIR}..."
    tar -xzf "${HAMMERDB_TARBALL}" -C "${SCRIPT_DIR}" || {
        log_error "Failed to extract HammerDB tarball"
        exit 1
    }

    # Clean up tarball
    rm -f "${HAMMERDB_TARBALL}"

    # Verify extraction
    if [ -f "${SCRIPT_DIR}/HammerDB-5.0/hammerdbcli" ]; then
        HAMMERDB_CLI="${SCRIPT_DIR}/HammerDB-5.0/hammerdbcli"
        log_info "HammerDB successfully downloaded and extracted: ${HAMMERDB_CLI}"
    else
        log_error "HammerDB extraction failed - hammerdbcli not found after extraction"
        exit 1
    fi
fi

# Verify hammerdb_load.tcl exists
if [ ! -f "${HAMMERDB_LOAD_TCL}" ]; then
    log_error "HammerDB load script not found: ${HAMMERDB_LOAD_TCL}"
    exit 1
fi

# 2. Set CPU governor to performance mode and disable CPU idle state
log_info "Setting CPU governor to performance mode..."
sudo cpupower frequency-set -g performance 2>/dev/null || log_warn "Could not set CPU governor (cpupower not available or insufficient permissions)"

log_info "Disabling CPU idle states..."
for cpu_idle in /sys/devices/system/cpu/cpu*/cpuidle/state*/disable; do
    if [ -f "$cpu_idle" ]; then
        echo 1 | sudo tee "$cpu_idle" > /dev/null 2>&1 || true
    fi
done

# 3. Create Server configuration file my.cnf
log_info "Creating MySQL configuration file: ${MY_CNF}"
cat > "${MY_CNF}" <<'EOF'
[mysqld]
# Server configuration for HammerDB TPC-C benchmark

# Data directory
datadir=DATA_DIR_PLACEHOLDER

# Disable binary logging
skip-log-bin

# InnoDB redo log configuration
innodb_redo_log_capacity = 32G

# Minimize flush overhead (not crash-safe, but optimal for testing)
innodb_flush_log_at_trx_commit = 0

# Memory configuration to target 150 GB buffer pool
innodb_buffer_pool_size = 150G
innodb_buffer_pool_instances = 16

# Connection settings
max_connections = 200

# Performance optimizations
innodb_flush_method = O_DIRECT
innodb_log_buffer_size = 256M
innodb_doublewrite = OFF

# Table settings
default-storage-engine = InnoDB

# Network settings
bind-address = 0.0.0.0
port = 3306

# Logging
log-error = ERROR_LOG_PLACEHOLDER
pid-file = PID_FILE_PLACEHOLDER

# SSL (optional, can be disabled for testing)
# ssl-ca = /path/to/ca.pem
# require_secure_transport = OFF

# Other settings
sql_mode = ""
EOF

# Replace placeholders
sed -i "s|DATA_DIR_PLACEHOLDER|${SERVER_DATA_DIR}|g" "${MY_CNF}"
sed -i "s|ERROR_LOG_PLACEHOLDER|${SERVER_DATA_DIR}/mysql-error.log|g" "${MY_CNF}"
sed -i "s|PID_FILE_PLACEHOLDER|${SERVER_DATA_DIR}/mysql.pid|g" "${MY_CNF}"

# 5.1. Add large-pages=ON if THP is enabled
if [ "${THP_ENABLED}" = "yes" ]; then
    log_info "Transparent Huge Pages enabled - adding large-pages=ON to my.cnf"
    sed -i '/innodb_buffer_pool_instances = 16/a\\n# Transparent Huge Pages\nlarge-pages = ON' "${MY_CNF}"
fi

log_info "Configuration file created successfully"

# 4. Ensure server data directory exists
if [ ! -d "${SERVER_DATA_DIR}" ]; then
    log_info "Creating server data directory: ${SERVER_DATA_DIR}"
    mkdir -p "${SERVER_DATA_DIR}"
fi

# Initialize data directory if empty
if [ ! -d "${SERVER_DATA_DIR}/mysql" ]; then
    log_info "Initializing MySQL data directory..."
    "${SERVER_BINARY}" --defaults-file="${MY_CNF}" --initialize-insecure --user=$(whoami)
fi

# Start MySQL server
log_info "Starting MySQL server..."
"${SERVER_BINARY}" --defaults-file="${MY_CNF}" --user=$(whoami) &
MYSQLD_PID=$!

# Wait for server to be ready
log_info "Waiting for MySQL server to be ready (PID: ${MYSQLD_PID})..."
sleep 5
MYSQL_CLIENT=$(dirname "${SERVER_BINARY}")/mysql
if [ ! -f "${MYSQL_CLIENT}" ]; then
    MYSQL_CLIENT=$(which mysql 2>/dev/null || echo "")
fi

if [ -z "${MYSQL_CLIENT}" ]; then
    log_error "MySQL client not found"
    kill ${MYSQLD_PID} 2>/dev/null || true
    exit 1
fi

for i in {1..30}; do
    if "${MYSQL_CLIENT}" -u root -e "SELECT 1" &>/dev/null; then
        log_info "MySQL server is ready"
        break
    fi
    if [ $i -eq 30 ]; then
        log_error "MySQL server failed to start"
        kill ${MYSQLD_PID} 2>/dev/null || true
        exit 1
    fi
    sleep 2
done

# 6. Check allocator
log_info "Checking allocator: ${ALLOCATOR}"
sleep 2  # Give mysqld a moment to fully load libraries

check_allocator() {
    local pid=$1
    local allocator=$2

    if [ ! -f "/proc/${pid}/maps" ]; then
        log_error "Process ${pid} not found"
        return 1
    fi

    case "${allocator}" in
        jemalloc36)
            if grep -q "libjemalloc.*3\.6" /proc/${pid}/maps; then
                log_info "Allocator check passed: jemalloc 3.6 is loaded"
                return 0
            else
                log_error "jemalloc 3.6 is not loaded in mysqld process"
                log_error ""
                log_error "To install and configure jemalloc 3.6 in Percona Server:"
                log_error "1. Install jemalloc 3.6: sudo apt-get install libjemalloc1 (or build from source)"
                log_error "2. Set LD_PRELOAD: export LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.1"
                log_error "3. Or add to systemd: Environment=\"LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.1\""
                log_error "4. Restart mysqld"
                return 1
            fi
            ;;
        jemalloc53)
            if grep -q "libjemalloc.*5\.3" /proc/${pid}/maps; then
                log_info "Allocator check passed: jemalloc 5.3 is loaded"
                return 0
            else
                log_error "jemalloc 5.3 is not loaded in mysqld process"
                log_error ""
                log_error "To install and configure jemalloc 5.3 in Percona Server:"
                log_error "1. Install jemalloc 5.3: sudo apt-get install libjemalloc2 (or build from source)"
                log_error "2. Set LD_PRELOAD: export LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2"
                log_error "3. Or add to systemd: Environment=\"LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2\""
                log_error "4. Restart mysqld with: LD_PRELOAD=/path/to/libjemalloc.so.2 ${SERVER_BINARY}"
                return 1
            fi
            ;;
        tcmalloc)
            if grep -q "libtcmalloc" /proc/${pid}/maps; then
                log_info "Allocator check passed: tcmalloc is loaded"
                return 0
            else
                log_error "tcmalloc is not loaded in mysqld process"
                log_error ""
                log_error "To install and configure tcmalloc in Percona Server:"
                log_error "1. Install tcmalloc: sudo apt-get install libgoogle-perftools-dev"
                log_error "2. Set LD_PRELOAD: export LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libtcmalloc.so.4"
                log_error "3. Or add to systemd: Environment=\"LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libtcmalloc.so.4\""
                log_error "4. Restart mysqld with: LD_PRELOAD=/path/to/libtcmalloc.so ${SERVER_BINARY}"
                return 1
            fi
            ;;
        glibc)
            if grep -q -E "libjemalloc|libtcmalloc" /proc/${pid}/maps; then
                log_error "glibc allocator requested, but jemalloc or tcmalloc is loaded"
                log_error "Remove LD_PRELOAD environment variable and restart mysqld"
                return 1
            else
                log_info "Allocator check passed: using glibc (no alternative allocator loaded)"
                return 0
            fi
            ;;
    esac
}

if ! check_allocator ${MYSQLD_PID} "${ALLOCATOR}"; then
    log_error "Allocator check failed, stopping server"
    kill ${MYSQLD_PID} 2>/dev/null || true
    exit 1
fi

# 7. Build the database using hammerdb_load.tcl
log_info "Building TPC-C database schema using HammerDB..."
log_info "This may take a while..."

"${HAMMERDB_CLI}" auto "${HAMMERDB_LOAD_TCL}" || {
    log_error "Database build failed"
    kill ${MYSQLD_PID} 2>/dev/null || true
    exit 1
}

log_info "Database build completed successfully"

# Create results directory
mkdir -p "${RESULTS_DIR}"

# Create HammerDB run script for TPC-C test
HAMMERDB_RUN_TCL="${SCRIPT_DIR}/hammerdb_run.tcl"
cat > "${HAMMERDB_RUN_TCL}" <<EOF
#!/usr/bin/tclsh
puts "SETTING CONFIGURATION FOR TPC-C RUN"
dbset db mysql
dbset bm TPC-C

diset connection localhost
diset connection mysql_port 3306
diset connection mysql_socket null
diset connection mysql_ssl false

diset tpcc mysql_user root
diset tpcc mysql_pass ""
diset tpcc mysql_dbase tpcc
diset tpcc mysql_driver timed
diset tpcc mysql_rampup 2
diset tpcc mysql_duration $(($BENCHMARK_DURATION_HOURS * 60))
diset tpcc mysql_allwarehouse true
diset tpcc mysql_timeprofile true

vuset vu ${VIRTUAL_USERS}
vucreate
vurun

puts "TPC-C TEST STARTED"
EOF

# 8. Run TPC-C test with 80 Virtual Users for 20 hours
log_info "Starting TPC-C benchmark: ${VIRTUAL_USERS} VUs for ${BENCHMARK_DURATION_HOURS} hours"

# Start HammerDB in background
"${HAMMERDB_CLI}" auto "${HAMMERDB_RUN_TCL}" > "${RESULTS_DIR}/hammerdb_output.log" 2>&1 &
HAMMERDB_PID=$!

# 9. Print remaining time every 10 seconds
# 10. Collect /proc/<pid>/status every 1 second
# 11. Collect /proc/<pid>/smaps_rollup every 1 second

log_info "Benchmark running (HammerDB PID: ${HAMMERDB_PID}, MySQL PID: ${MYSQLD_PID})"
log_info "Results directory: ${RESULTS_DIR}"

START_TIME=$(date +%s)
END_TIME=$((START_TIME + BENCHMARK_DURATION_HOURS * 3600))

STATUS_FILE="${RESULTS_DIR}/mysql_status_$(date +%Y%m%d_%H%M%S).log"
SMAPS_FILE="${RESULTS_DIR}/mysql_smaps_rollup_$(date +%Y%m%d_%H%M%S).log"

# Add headers
echo "# MySQL /proc/${MYSQLD_PID}/status data collection" > "${STATUS_FILE}"
echo "# Started at: $(date)" >> "${STATUS_FILE}"
echo "" >> "${STATUS_FILE}"

echo "# MySQL /proc/${MYSQLD_PID}/smaps_rollup data collection" > "${SMAPS_FILE}"
echo "# Started at: $(date)" >> "${SMAPS_FILE}"
echo "" >> "${SMAPS_FILE}"

# Background data collection processes
collect_proc_data() {
    local pid=$1
    local status_file=$2
    local smaps_file=$3

    while kill -0 ${pid} 2>/dev/null && kill -0 ${HAMMERDB_PID} 2>/dev/null; do
        TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

        # Collect status
        if [ -f "/proc/${pid}/status" ]; then
            echo "=== ${TIMESTAMP} ===" >> "${status_file}"
            cat /proc/${pid}/status >> "${status_file}" 2>/dev/null || true
            echo "" >> "${status_file}"
        fi

        # Collect smaps_rollup
        if [ -f "/proc/${pid}/smaps_rollup" ]; then
            echo "=== ${TIMESTAMP} ===" >> "${smaps_file}"
            cat /proc/${pid}/smaps_rollup >> "${smaps_file}" 2>/dev/null || true
            echo "" >> "${smaps_file}"
        fi

        sleep 1
    done
}

# Start data collection in background
collect_proc_data ${MYSQLD_PID} "${STATUS_FILE}" "${SMAPS_FILE}" &
COLLECTOR_PID=$!

# Time reporting loop
LAST_REPORT=0
while kill -0 ${HAMMERDB_PID} 2>/dev/null; do
    CURRENT_TIME=$(date +%s)
    ELAPSED=$((CURRENT_TIME - START_TIME))
    REMAINING=$((END_TIME - CURRENT_TIME))

    if [ $REMAINING -lt 0 ]; then
        REMAINING=0
    fi

    # Report every 10 seconds
    if [ $((ELAPSED - LAST_REPORT)) -ge 10 ]; then
        HOURS=$((REMAINING / 3600))
        MINUTES=$(((REMAINING % 3600) / 60))
        SECONDS=$((REMAINING % 60))

        log_info "Benchmark progress - Time remaining: ${HOURS}h ${MINUTES}m ${SECONDS}s"
        LAST_REPORT=$ELAPSED
    fi

    sleep 1
done

# Wait for HammerDB to complete
wait ${HAMMERDB_PID}
HAMMERDB_EXIT=$?

# Stop data collection
kill ${COLLECTOR_PID} 2>/dev/null || true
wait ${COLLECTOR_PID} 2>/dev/null || true

log_info "Benchmark completed (exit code: ${HAMMERDB_EXIT})"

# Stop MySQL server
log_info "Stopping MySQL server..."
kill ${MYSQLD_PID} 2>/dev/null || true
wait ${MYSQLD_PID} 2>/dev/null || true

# Summary
log_info "======================================"
log_info "Benchmark Summary"
log_info "======================================"
log_info "Server binary: ${SERVER_BINARY}"
log_info "Allocator: ${ALLOCATOR}"
log_info "THP enabled: ${THP_ENABLED}"
log_info "Virtual Users: ${VIRTUAL_USERS}"
log_info "Duration: ${BENCHMARK_DURATION_HOURS} hours"
log_info "Results directory: ${RESULTS_DIR}"
log_info "  - HammerDB output: ${RESULTS_DIR}/hammerdb_output.log"
log_info "  - MySQL status data: ${STATUS_FILE}"
log_info "  - MySQL smaps_rollup data: ${SMAPS_FILE}"
log_info "======================================"

exit ${HAMMERDB_EXIT}
