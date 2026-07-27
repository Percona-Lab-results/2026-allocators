#!/bin/bash
set -euo pipefail

# Standalone runner for sp_cursor_memtest: initializes a fresh data
# directory, starts mysqld with a my.cnf modeled on the HammerDB benchmark
# one, creates the tpcuser account, runs the cursor memory test while
# logging mysqld RSS/VSZ every 30 seconds, then shuts the server down.
#
# Usage:
#   ./run_sp_cursor_memtest.sh [--server=/path/to/mysqld] [--datadir=PATH]
#       [--buffer-gb=N] [--threads=N] [--duration=N] [--inner-iters=N]
#
# Defaults:
#   --server      ${HOME}/servers/Percona-Server-8.4.8-8-Linux.x86_64.glibc2.35/bin/mysqld
#   --datadir     /home/bogdan.degtyariov/servers/data  (CLEARED before init!)
#   --buffer-gb   8
#   --threads     16
#   --duration    900 (seconds)
#   --inner-iters 100 (cursor opens per CALL)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SERVER_BINARY="${HOME}/servers/Percona-Server-8.4.8-8-Linux.x86_64.glibc2.35/bin/mysqld"
SERVER_DATA_DIR="/home/bogdan.degtyariov/servers/data"
BUFFER_POOL_SIZE_GB=80
TEST_THREADS=64
TEST_DURATION=900
TEST_INNER_ITERS=100

MYSQL_SOCKET="/tmp/mysql-cursor-test.sock"
MY_CNF="${SCRIPT_DIR}/my-cursor-test.cnf"
MEMTEST_BIN="${SCRIPT_DIR}/sp_cursor_memtest"
RSS_LIMIT_KB=$((178 * 1024 * 1024))  # 178 GB: abort the test if mysqld RSS exceeds this

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} [$(date +"%Y-%m-%d %H:%M:%S")] $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} [$(date +"%Y-%m-%d %H:%M:%S")] $1" >&2; }

for arg in "$@"; do
    case $arg in
        --server=*)      SERVER_BINARY="${arg#*=}" ;;
        --datadir=*)     SERVER_DATA_DIR="${arg#*=}" ;;
        --buffer-gb=*)   BUFFER_POOL_SIZE_GB="${arg#*=}" ;;
        --threads=*)     TEST_THREADS="${arg#*=}" ;;
        --duration=*)    TEST_DURATION="${arg#*=}" ;;
        --inner-iters=*) TEST_INNER_ITERS="${arg#*=}" ;;
        *) log_error "Unknown argument: $arg"; exit 1 ;;
    esac
done

if [ ! -f "${SERVER_BINARY}" ]; then
    log_error "Server binary not found: ${SERVER_BINARY}"
    exit 1
fi

if [ ! -x "${MEMTEST_BIN}" ]; then
    log_error "sp_cursor_memtest binary not found: ${MEMTEST_BIN}"
    log_error "Build it first (see header comment in sp_cursor_memtest.cpp)"
    exit 1
fi

MYSQL_CLIENT="$(dirname "${SERVER_BINARY}")/mysql"
if [ ! -x "${MYSQL_CLIENT}" ]; then
    log_error "mysql client not found: ${MYSQL_CLIENT}"
    exit 1
fi

DATE_TIME=$(date +%Y%m%d_%H%M%S)
RESULTS_DIR="${SCRIPT_DIR}/results-sp-cursor-memtest-${DATE_TIME}"
mkdir -p "${RESULTS_DIR}"

RSS_LOG="${RESULTS_DIR}/sp_cursor_memtest_rss_${DATE_TIME}.log"
STATUS_FILE="${RESULTS_DIR}/mysql_status_${DATE_TIME}.log"
SMAPS_ROLLUP_FILE="${RESULTS_DIR}/mysql_smaps_rollup_${DATE_TIME}.log"
SMAPS_FILE="${RESULTS_DIR}/mysql_smaps_${DATE_TIME}.log"
STAT_FILE="${RESULTS_DIR}/mysql_stat_${DATE_TIME}.log"
MAPS_FILE="${RESULTS_DIR}/mysql_maps_${DATE_TIME}.log"

# 1. Clear and initialize the data directory
if [ -d "${SERVER_DATA_DIR}" ]; then
    log_info "Removing old data directory: ${SERVER_DATA_DIR}"
    rm -rf "${SERVER_DATA_DIR}"
fi
mkdir -p "${SERVER_DATA_DIR}"

log_info "Initializing MySQL data directory..."
"${SERVER_BINARY}" --no-defaults --initialize-insecure --user=$(whoami) \
    --datadir="${SERVER_DATA_DIR}"

# 2. Create my.cnf (modeled on the HammerDB benchmark configuration)
log_info "Creating configuration file: ${MY_CNF}"
cat > "${MY_CNF}" <<EOF
[mysqld]
# Server configuration for sp_cursor_memtest

# Data directory
datadir=${SERVER_DATA_DIR}

# Disable binary logging
skip-log-bin

# InnoDB redo log configuration
innodb_redo_log_capacity = 32G

# Minimize flush overhead (not crash-safe, but optimal for testing)
innodb_flush_log_at_trx_commit = 0

# Memory configuration
innodb_buffer_pool_size = ${BUFFER_POOL_SIZE_GB}G
innodb_buffer_pool_instances = 16
innodb_io_capacity = 20000

# Connection settings
max_connections = 200

# Performance optimizations
innodb_flush_method = O_DIRECT
innodb_log_buffer_size = 256M
innodb_doublewrite = OFF

# Table settings
default-storage-engine = InnoDB

# Logging
log-error = ${SERVER_DATA_DIR}/mysql-error.log
pid-file = ${SERVER_DATA_DIR}/mysql.pid

# Socket
socket = ${MYSQL_SOCKET}

# Disable SSL requirement
require_secure_transport = OFF

# Disable secure-file-priv restriction
secure-file-priv = ""

# Other settings
sql_mode = ""
wait_timeout = 288000        # 80 hours
interactive_timeout = 288000 # 80 hours
EOF

# 3. Start the server
log_info "Starting MySQL server..."
"${SERVER_BINARY}" --defaults-file="${MY_CNF}" --user=$(whoami) &
MYSQLD_PID=$!

stop_server() {
    if kill -0 ${MYSQLD_PID} 2>/dev/null; then
        log_info "Stopping MySQL server (PID: ${MYSQLD_PID})..."
        kill ${MYSQLD_PID} 2>/dev/null || true
        wait ${MYSQLD_PID} 2>/dev/null || true
    fi
}

RSS_LOGGER_PID=""
MEMTEST_PID=""
COLLECTOR_PID=""
cleanup() {
    [ -n "${MEMTEST_PID}" ] && kill ${MEMTEST_PID} 2>/dev/null || true
    [ -n "${RSS_LOGGER_PID}" ] && kill ${RSS_LOGGER_PID} 2>/dev/null || true
    [ -n "${COLLECTOR_PID}" ] && kill ${COLLECTOR_PID} 2>/dev/null || true
    stop_server
}
trap cleanup INT TERM EXIT

log_info "Waiting for server to be ready (PID: ${MYSQLD_PID})..."
for i in {1..120}; do
    if ! kill -0 ${MYSQLD_PID} 2>/dev/null; then
        log_error "mysqld died during startup, check ${SERVER_DATA_DIR}/mysql-error.log"
        exit 1
    fi
    if "${MYSQL_CLIENT}" --socket="${MYSQL_SOCKET}" -u root -e "SELECT 1" >/dev/null 2>&1; then
        log_info "MySQL server is ready"
        break
    fi
    if [ $i -eq 120 ]; then
        log_error "Server did not become ready in 240 seconds"
        exit 1
    fi
    sleep 2
done

# 4. Create tpcuser
log_info "Creating user tpcuser..."
"${MYSQL_CLIENT}" --socket="${MYSQL_SOCKET}" -u root <<EOF
CREATE USER IF NOT EXISTS 'tpcuser'@'%' IDENTIFIED BY 'tpcpass';
GRANT ALL PRIVILEGES ON *.* TO 'tpcuser'@'%' WITH GRANT OPTION;
CREATE USER IF NOT EXISTS 'tpcuser'@'localhost' IDENTIFIED BY 'tpcpass';
GRANT ALL PRIVILEGES ON *.* TO 'tpcuser'@'localhost' WITH GRANT OPTION;
FLUSH PRIVILEGES;
EOF

# 5. /proc/<pid> data collection (same as run_hammerdb_benchmark.sh:
# status, smaps_rollup, stat, maps every 1 second; smaps every 30 seconds)
log_info "Collecting /proc/${MYSQLD_PID}/ data into: ${RESULTS_DIR}"

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

collect_proc_data() {
    local pid=$1
    local iteration=0

    while kill -0 ${pid} 2>/dev/null; do
        TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

        # Collect status (every 1 second)
        if [ -f "/proc/${pid}/status" ]; then
            echo "=== ${TIMESTAMP} ===" >> "${STATUS_FILE}"
            cat /proc/${pid}/status >> "${STATUS_FILE}" 2>/dev/null || true
            echo "" >> "${STATUS_FILE}"
        fi

        # Collect smaps_rollup (every 1 second)
        if [ -f "/proc/${pid}/smaps_rollup" ]; then
            echo "=== ${TIMESTAMP} ===" >> "${SMAPS_ROLLUP_FILE}"
            cat /proc/${pid}/smaps_rollup >> "${SMAPS_ROLLUP_FILE}" 2>/dev/null || true
            echo "" >> "${SMAPS_ROLLUP_FILE}"
        fi

        # Collect stat (every 1 second)
        if [ -f "/proc/${pid}/stat" ]; then
            echo "=== ${TIMESTAMP} ===" >> "${STAT_FILE}"
            cat /proc/${pid}/stat >> "${STAT_FILE}" 2>/dev/null || true
            echo "" >> "${STAT_FILE}"
        fi

        # Collect maps (every 1 second)
        if [ -f "/proc/${pid}/maps" ]; then
            echo "=== ${TIMESTAMP} ===" >> "${MAPS_FILE}"
            cat /proc/${pid}/maps >> "${MAPS_FILE}" 2>/dev/null || true
            echo "" >> "${MAPS_FILE}"
        fi

        # Collect smaps (every 30 seconds)
        if [ $((iteration % 30)) -eq 0 ]; then
            if [ -f "/proc/${pid}/smaps" ]; then
                echo "=== ${TIMESTAMP} ===" >> "${SMAPS_FILE}"
                cat /proc/${pid}/smaps >> "${SMAPS_FILE}" 2>/dev/null || true
                echo "" >> "${SMAPS_FILE}"
            fi
        fi

        iteration=$((iteration + 1))
        sleep 1
    done
}

collect_proc_data ${MYSQLD_PID} &
COLLECTOR_PID=$!

# 6. RSS/VSZ logger: sample mysqld memory every 30 seconds
# (separate from collect_proc_data because it also enforces the RSS limit)
log_info "Logging mysqld RSS/VSZ every 30 seconds to: ${RSS_LOG}"
echo "# mysqld memory log (every 30 seconds), PID ${MYSQLD_PID}" > "${RSS_LOG}"
echo "# Timestamp, VmRSS_KB, VmSize_KB" >> "${RSS_LOG}"

# The logger also enforces the RSS limit: if mysqld RSS exceeds
# RSS_LIMIT_KB it kills the memtest (the main script then shuts the
# server down through its normal path)
rss_logger() {
    local pid=$1
    local memtest_pid=$2
    while kill -0 ${pid} 2>/dev/null; do
        local rss vsz
        rss=$(grep '^VmRSS:'  /proc/${pid}/status 2>/dev/null | awk '{print $2}')
        vsz=$(grep '^VmSize:' /proc/${pid}/status 2>/dev/null | awk '{print $2}')
        echo "$(date +"%Y-%m-%d %H:%M:%S"), ${rss:-0}, ${vsz:-0}" >> "${RSS_LOG}"

        if [ "${rss:-0}" -gt "${RSS_LIMIT_KB}" ]; then
            log_error "mysqld RSS ($((rss / 1024 / 1024)) GB) exceeded limit of $((RSS_LIMIT_KB / 1024 / 1024)) GB!"
            log_error "Terminating test due to memory limit"
            echo "# RSS limit exceeded, test terminated" >> "${RSS_LOG}"
            kill ${memtest_pid} 2>/dev/null || true
            return
        fi

        sleep 30
    done
}

# 7. Run the cursor memory test (in background so the RSS logger can
# terminate it if the memory limit is exceeded)
log_info "Running sp_cursor_memtest (threads=${TEST_THREADS}, duration=${TEST_DURATION}s, inner-iters=${TEST_INNER_ITERS})..."
log_info "RSS limit: $((RSS_LIMIT_KB / 1024 / 1024)) GB"
"${MEMTEST_BIN}" --socket="${MYSQL_SOCKET}" --user=tpcuser --password=tpcpass \
    --threads="${TEST_THREADS}" --duration="${TEST_DURATION}" \
    --inner-iters="${TEST_INNER_ITERS}" --report=30 &
MEMTEST_PID=$!

rss_logger ${MYSQLD_PID} ${MEMTEST_PID} &
RSS_LOGGER_PID=$!

set +e
wait ${MEMTEST_PID}
MEMTEST_EXIT=$?
set -e

if [ ${MEMTEST_EXIT} -ne 0 ]; then
    log_error "sp_cursor_memtest exited with code ${MEMTEST_EXIT}"
fi

# 8. Stop the collectors, then compute memory growth between the first
# sample and the last sample taken while the test was still running
# (the server is still up here, but no further samples are appended)
[ -n "${RSS_LOGGER_PID}" ] && kill ${RSS_LOGGER_PID} 2>/dev/null || true
wait ${RSS_LOGGER_PID} 2>/dev/null || true
RSS_LOGGER_PID=""

[ -n "${COLLECTOR_PID}" ] && kill ${COLLECTOR_PID} 2>/dev/null || true
wait ${COLLECTOR_PID} 2>/dev/null || true
COLLECTOR_PID=""

FIRST_SAMPLE=$(grep -v '^#' "${RSS_LOG}" | head -1)
LAST_SAMPLE=$(grep -v '^#' "${RSS_LOG}" | tail -1)

if [ -n "${FIRST_SAMPLE}" ] && [ -n "${LAST_SAMPLE}" ] && [ "${FIRST_SAMPLE}" != "${LAST_SAMPLE}" ]; then
    FIRST_TS=$(echo "${FIRST_SAMPLE}" | cut -d',' -f1)
    FIRST_RSS=$(echo "${FIRST_SAMPLE}" | cut -d',' -f2 | tr -d ' ')
    FIRST_VSZ=$(echo "${FIRST_SAMPLE}" | cut -d',' -f3 | tr -d ' ')
    LAST_TS=$(echo "${LAST_SAMPLE}" | cut -d',' -f1)
    LAST_RSS=$(echo "${LAST_SAMPLE}" | cut -d',' -f2 | tr -d ' ')
    LAST_VSZ=$(echo "${LAST_SAMPLE}" | cut -d',' -f3 | tr -d ' ')

    ELAPSED_SEC=$(( $(date -d "${LAST_TS}" +%s) - $(date -d "${FIRST_TS}" +%s) ))
    RSS_DELTA_KB=$((LAST_RSS - FIRST_RSS))
    VSZ_DELTA_KB=$((LAST_VSZ - FIRST_VSZ))

    log_info "======================================"
    log_info "Memory growth summary (mysqld)"
    log_info "======================================"
    log_info "First sample: ${FIRST_TS} (RSS $((FIRST_RSS / 1024)) MB, VSZ $((FIRST_VSZ / 1024)) MB)"
    log_info "Last sample:  ${LAST_TS} (RSS $((LAST_RSS / 1024)) MB, VSZ $((LAST_VSZ / 1024)) MB)"
    log_info "Elapsed: ${ELAPSED_SEC} seconds"
    log_info "RSS delta: ${RSS_DELTA_KB} KB ($((RSS_DELTA_KB / 1024)) MB)"
    log_info "VSZ delta: ${VSZ_DELTA_KB} KB ($((VSZ_DELTA_KB / 1024)) MB)"
    if [ ${ELAPSED_SEC} -gt 0 ]; then
        RSS_RATE=$(awk "BEGIN {printf \"%.1f\", ${RSS_DELTA_KB} / ${ELAPSED_SEC}}")
        VSZ_RATE=$(awk "BEGIN {printf \"%.1f\", ${VSZ_DELTA_KB} / ${ELAPSED_SEC}}")
        log_info "RSS growth rate: ${RSS_RATE} KB/s ($(awk "BEGIN {printf \"%.2f\", ${RSS_DELTA_KB} / ${ELAPSED_SEC} / 1024}") MB/s)"
        log_info "VSZ growth rate: ${VSZ_RATE} KB/s ($(awk "BEGIN {printf \"%.2f\", ${VSZ_DELTA_KB} / ${ELAPSED_SEC} / 1024}") MB/s)"
    fi
    log_info "======================================"
    {
        echo ""
        echo "# Summary: elapsed=${ELAPSED_SEC}s rss_delta_kb=${RSS_DELTA_KB} vsz_delta_kb=${VSZ_DELTA_KB}"
        [ ${ELAPSED_SEC} -gt 0 ] && echo "# rss_rate_kb_per_s=${RSS_RATE:-n/a} vsz_rate_kb_per_s=${VSZ_RATE:-n/a}"
    } >> "${RSS_LOG}"
else
    log_error "Not enough samples in ${RSS_LOG} to compute memory growth (need at least 2)"
fi

# 9. Shut down (trap also covers abnormal exits)
stop_server
trap - INT TERM EXIT

log_info "Results directory: ${RESULTS_DIR}"
log_info "  - RSS/VSZ memory log: ${RSS_LOG}"
log_info "  - MySQL status data: ${STATUS_FILE}"
log_info "  - MySQL smaps_rollup data: ${SMAPS_ROLLUP_FILE}"
log_info "  - MySQL smaps data: ${SMAPS_FILE}"
log_info "  - MySQL stat data: ${STAT_FILE}"
log_info "  - MySQL maps data: ${MAPS_FILE}"
exit ${MEMTEST_EXIT}
