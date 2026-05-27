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
MYSQL_SOCKET="/tmp/mysql-alloc-test.sock"
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

# Kill any existing mysqld processes
log_info "Killing any existing mysqld processes..."
sudo killall mysqld 2>/dev/null || true
sleep 2

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

    HAMMERDB_URL="https://github.com/TPC-Council/HammerDB/releases/download/v5.0/HammerDB-5.0-Prod-Lin-UBU22.tar.gz"
    HAMMERDB_TARBALL="${SCRIPT_DIR}/HammerDB-5.0-Prod-Lin-UBU22.tar.gz"

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

# Logging
log-error = ERROR_LOG_PLACEHOLDER
pid-file = PID_FILE_PLACEHOLDER

# Socket
socket = SOCKET_PLACEHOLDER

# Disable SSL requirement
require_secure_transport = OFF

# Other settings
sql_mode = ""
EOF

# Replace placeholders
sed -i "s|DATA_DIR_PLACEHOLDER|${SERVER_DATA_DIR}|g" "${MY_CNF}"
sed -i "s|ERROR_LOG_PLACEHOLDER|${SERVER_DATA_DIR}/mysql-error.log|g" "${MY_CNF}"
sed -i "s|PID_FILE_PLACEHOLDER|${SERVER_DATA_DIR}/mysql.pid|g" "${MY_CNF}"
sed -i "s|SOCKET_PLACEHOLDER|${MYSQL_SOCKET}|g" "${MY_CNF}"

# 5.1. Add large-pages=ON if THP is enabled
if [ "${THP_ENABLED}" = "yes" ]; then
    log_info "Transparent Huge Pages enabled - adding large-pages=ON to my.cnf"
    sed -i '/innodb_buffer_pool_instances = 16/a\\n# Transparent Huge Pages\nlarge-pages = ON' "${MY_CNF}"
fi

log_info "Configuration file created successfully"

# 4. Remove old server data directory and create fresh one
if [ -d "${SERVER_DATA_DIR}" ]; then
   log_info "Removing old server data directory: ${SERVER_DATA_DIR}"
   rm -rf "${SERVER_DATA_DIR}"
fi

log_info "Creating fresh server data directory: ${SERVER_DATA_DIR}"
mkdir -p "${SERVER_DATA_DIR}"

# Initialize data directory
log_info "Initializing MySQL data directory..."
"${SERVER_BINARY}" --defaults-file="${MY_CNF}" --initialize-insecure --user=$(whoami) \
 --innodb_flush_log_at_trx_commit=0 \
 --innodb_doublewrite=0 \
 --sync_binlog=0 \
 --innodb_buffer_pool_size=1G

# Ensure data directory exists (when skipping initialization)
#mkdir -p "${SERVER_DATA_DIR}"

# Set LD_PRELOAD for jemalloc36 if specified
if [ "${ALLOCATOR}" = "jemalloc36" ]; then
    SERVER_DIR=$(dirname "$(dirname "${SERVER_BINARY}")")
    JEMALLOC_LIB="${SERVER_DIR}/lib/mysql/libjemalloc.so.1"

    if [ -f "${JEMALLOC_LIB}" ]; then
        export LD_PRELOAD="${JEMALLOC_LIB}"
        log_info "LD_PRELOAD set to: ${LD_PRELOAD}"
    else
        log_error "jemalloc36 library not found at: ${JEMALLOC_LIB}"
        exit 1
    fi
fi

# Start MySQL server
log_info "Starting MySQL server..."
log_info "Command: ${SERVER_BINARY} --defaults-file=${MY_CNF} --user=$(whoami)"
"${SERVER_BINARY}" --defaults-file="${MY_CNF}" --user=$(whoami) &
MYSQLD_PID=$!

# Set OOM score adjustment to protect mysqld from OOM killer
log_info "Setting OOM score adjustment to -900 for mysqld (PID: ${MYSQLD_PID})..."
echo -900 | sudo tee /proc/${MYSQLD_PID}/oom_score_adj > /dev/null || log_warn "Failed to set OOM score adjustment"

# Wait for server to be ready
log_info "Waiting for MySQL server to be ready (PID: ${MYSQLD_PID})..."
sleep 5
MYSQL_CLIENT=$(dirname "${SERVER_BINARY}")/mysql

log_info "MySQL client path: ${MYSQL_CLIENT}"

if [ -z "${MYSQL_CLIENT}" ]; then
    log_error "MySQL client not found"
    kill ${MYSQLD_PID} 2>/dev/null || true
    exit 1
fi

for i in {1..300}; do
    log_info "Connecting to mysql client..."
    set +e
    CONNECT_OUTPUT=$("${MYSQL_CLIENT}" --socket="${MYSQL_SOCKET}" -u root -e "SELECT 1" 2>&1)
    MYSQL_EXIT_CODE=$?
    set -e
    log_info "OUT=$CONNECT_OUTPUT"
    if [ $MYSQL_EXIT_CODE -eq 0 ]; then
        log_info "MySQL server is ready"
        break
    fi
    if [ $i -eq 60 ]; then
        log_error "MySQL server failed to start after 120 seconds"
        log_error "Last connection error: ${CONNECT_OUTPUT}"
        log_error "Check error log: ${SERVER_DATA_DIR}/mysql-error.log"
        kill ${MYSQLD_PID} 2>/dev/null || true
        exit 1
    fi
    log_warn "Connection attempt $i/60 failed, retrying... (${CONNECT_OUTPUT})"
    sleep 2
done

# Create MySQL user for HammerDB
log_info "Creating MySQL user for HammerDB..."
"${MYSQL_CLIENT}" --socket="${MYSQL_SOCKET}" -u root <<EOF
CREATE USER IF NOT EXISTS 'tpcuser'@'%' IDENTIFIED BY 'tpcpass';
GRANT ALL PRIVILEGES ON *.* TO 'tpcuser'@'%' WITH GRANT OPTION;
CREATE USER IF NOT EXISTS 'tpcuser'@'localhost' IDENTIFIED BY 'tpcpass';
GRANT ALL PRIVILEGES ON *.* TO 'tpcuser'@'localhost' WITH GRANT OPTION;
FLUSH PRIVILEGES;
EOF

if [ $? -eq 0 ]; then
    log_info "MySQL user 'tpcuser' created successfully with all privileges"
else
    log_error "Failed to create MySQL user"
    kill ${MYSQLD_PID} 2>/dev/null || true
    exit 1
fi

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
            if grep -q "libjemalloc\.so\.1" /proc/${pid}/maps; then
                log_info "Allocator check passed: libjemalloc.so.1 is loaded"
                return 0
            else
                log_error "libjemalloc.so.1 is not loaded in mysqld process"
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

log_info "Skipping database build - using existing database"

# Create results directory
mkdir -p "${RESULTS_DIR}"

# Create HammerDB run script for TPC-C test
HAMMERDB_RUN_TCL="${SCRIPT_DIR}/hammerdb_run.tcl"
BENCHMARK_DURATION_MINUTES=$((BENCHMARK_DURATION_HOURS * 60))
cat > "${HAMMERDB_RUN_TCL}" <<EOF
#!/usr/bin/tclsh
puts "SETTING CONFIGURATION FOR TPC-C RUN"
dbset db mysql
dbset bm TPC-C

puts "Setting connection parameters..."
diset connection mysql_host localhost
diset connection mysql_socket /tmp/mysql-alloc-test.sock
diset connection mysql_ssl false

puts "Setting TPC-C parameters..."
diset tpcc mysql_user tpcuser
diset tpcc mysql_pass tpcpass
diset tpcc mysql_dbase tpcc
diset tpcc mysql_driver timed
diset tpcc mysql_rampup 2
diset tpcc mysql_duration ${BENCHMARK_DURATION_MINUTES}
diset tpcc mysql_allwarehouse true
diset tpcc mysql_timeprofile true
diset tpcc mysql_history_pk true

puts "Printing current configuration..."
print dict

puts "Creating ${VIRTUAL_USERS} virtual users..."
vuset vu ${VIRTUAL_USERS}

puts "Creating virtual user threads..."
vucreate

puts "Running virtual users..."
if {[catch {vurun} result]} {
    puts "ERROR during vurun: \$result"
    puts "Error Info: \$::errorInfo"
    exit 1
}

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

DATE_TIME=$(date +%Y%m%d_%H%M%S)
STATUS_FILE="${RESULTS_DIR}/mysql_status_${DATE_TIME}.log"
SMAPS_ROLLUP_FILE="${RESULTS_DIR}/mysql_smaps_rollup_${DATE_TIME}.log"
SMAPS_FILE="${RESULTS_DIR}/mysql_smaps_${DATE_TIME}.log"
STAT_FILE="${RESULTS_DIR}/mysql_stat_${DATE_TIME}.log"
MAPS_FILE="${RESULTS_DIR}/mysql_maps_${DATE_TIME}.log"

# Add headers
echo "# MySQL /proc/${MYSQLD_PID}/status data collection" > "${STATUS_FILE}"
echo "# Started at: $(date)" >> "${STATUS_FILE}"
echo "" >> "${STATUS_FILE}"

echo "# MySQL /proc/${MYSQLD_PID}/smaps_rollup data collection" > "${SMAPS_ROLLUP_FILE}"
echo "# Started at: $(date)" >> "${SMAPS_ROLLUP_FILE}"
echo "" >> "${SMAPS_ROLLUP_FILE}"

echo "# MySQL /proc/${MYSQLD_PID}/smaps data collection (every 30 seconds)" > "${SMAPS_FILE}"
echo "# Started at: $(date)" >> "${SMAPS_FILE}"
echo "" >> "${SMAPS_FILE}"

echo "# MySQL /proc/${MYSQLD_PID}/stat data collection" > "${STAT_FILE}"
echo "# Started at: $(date)" >> "${STAT_FILE}"
echo "" >> "${STAT_FILE}"

echo "# MySQL /proc/${MYSQLD_PID}/maps data collection" > "${MAPS_FILE}"
echo "# Started at: $(date)" >> "${MAPS_FILE}"
echo "" >> "${MAPS_FILE}"

# Background data collection processes
collect_proc_data() {
    local pid=$1
    local status_file=$2
    local smaps_rollup_file=$3
    local smaps_file=$4
    local stat_file=$5
    local maps_file=$6

    local iteration=0

    while kill -0 ${pid} 2>/dev/null && kill -0 ${HAMMERDB_PID} 2>/dev/null; do
        TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

        # Collect status (every 1 second)
        if [ -f "/proc/${pid}/status" ]; then
            echo "=== ${TIMESTAMP} ===" >> "${status_file}"
            cat /proc/${pid}/status >> "${status_file}" 2>/dev/null || true
            echo "" >> "${status_file}"
        fi

        # Collect smaps_rollup (every 1 second)
        if [ -f "/proc/${pid}/smaps_rollup" ]; then
            echo "=== ${TIMESTAMP} ===" >> "${smaps_rollup_file}"
            cat /proc/${pid}/smaps_rollup >> "${smaps_rollup_file}" 2>/dev/null || true
            echo "" >> "${smaps_rollup_file}"
        fi

        # Collect stat (every 1 second)
        if [ -f "/proc/${pid}/stat" ]; then
            echo "=== ${TIMESTAMP} ===" >> "${stat_file}"
            cat /proc/${pid}/stat >> "${stat_file}" 2>/dev/null || true
            echo "" >> "${stat_file}"
        fi

        # Collect maps (every 1 second)
        if [ -f "/proc/${pid}/maps" ]; then
            echo "=== ${TIMESTAMP} ===" >> "${maps_file}"
            cat /proc/${pid}/maps >> "${maps_file}" 2>/dev/null || true
            echo "" >> "${maps_file}"
        fi

        # Collect smaps (every 30 seconds)
        if [ $((iteration % 30)) -eq 0 ]; then
            if [ -f "/proc/${pid}/smaps" ]; then
                echo "=== ${TIMESTAMP} ===" >> "${smaps_file}"
                cat /proc/${pid}/smaps >> "${smaps_file}" 2>/dev/null || true
                echo "" >> "${smaps_file}"
            fi
        fi

        iteration=$((iteration + 1))
        sleep 1
    done
}

# Start data collection in background
collect_proc_data ${MYSQLD_PID} "${STATUS_FILE}" "${SMAPS_ROLLUP_FILE}" "${SMAPS_FILE}" "${STAT_FILE}" "${MAPS_FILE}" &
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
log_info "  - MySQL smaps_rollup data: ${SMAPS_ROLLUP_FILE}"
log_info "  - MySQL smaps data: ${SMAPS_FILE}"
log_info "  - MySQL stat data: ${STAT_FILE}"
log_info "  - MySQL maps data: ${MAPS_FILE}"
log_info "======================================"

exit ${HAMMERDB_EXIT}
