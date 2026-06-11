#!/bin/bash
set -euo pipefail

# HammerDB TPC-C Benchmark Script for MySQL/Percona Server
# Usage: ./run_hammerdb_benchmark.sh <server_binary_path> <thp:thp|nothp> <allocator:jemalloc36|jemalloc53|tcmalloc|glibc> <skip_init:skip|noskip> <buffer_pool_size_gb> <suffix> <enable_binlog:binlog|nobinlog> <storage_engine:innodb|myrocks>
#
# Examples:
#   ./run_hammerdb_benchmark.sh /opt/percona/bin/mysqld thp jemalloc53 noskip 110 test1 nobinlog innodb
#   ./run_hammerdb_benchmark.sh /opt/percona/bin/mysqld thp jemalloc53 skip 110 test2 binlog innodb
#   ./run_hammerdb_benchmark.sh /opt/percona/bin/mysqld nothp tcmalloc noskip 64 test3 nobinlog myrocks

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DATA_DIR="${HOME}/servers/data"
MY_CNF="${SCRIPT_DIR}/my.cnf"
HAMMERDB_LOAD_TCL="${SCRIPT_DIR}/hammerdb_load.tcl"
MYSQL_SOCKET="/tmp/mysql-alloc-test.sock"
BENCHMARK_DURATION_MINUTES=1200  # 20 hours = 1200 minutes
RAMPUP_DURATION_MINUTES=15       # Ramp-up time before benchmark starts
VIRTUAL_USERS=80

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    local timestamp=$(date +"%Y-%m-%d %H:%M:%S")
    echo -e "${GREEN}[INFO]${NC} [${timestamp}] $1"
}

log_error() {
    local timestamp=$(date +"%Y-%m-%d %H:%M:%S")
    echo -e "${RED}[ERROR]${NC} [${timestamp}] $1" >&2
}

log_warn() {
    local timestamp=$(date +"%Y-%m-%d %H:%M:%S")
    echo -e "${YELLOW}[WARN]${NC} [${timestamp}] $1"
}

# Kill any existing mysqld processes
log_info "Killing any existing mysqld processes..."
sudo killall mysqld 2>/dev/null || true
sleep 2

# Check command line arguments
if [ $# -ne 8 ]; then
    log_error "Usage: $0 <server_binary_path> <thp:thp|nothp> <allocator:jemalloc36|jemalloc53|tcmalloc|glibc> <skip_init:skip|noskip> <buffer_pool_size_gb> <suffix> <enable_binlog:binlog|nobinlog> <storage_engine:innodb|myrocks>"
    log_error "Example: $0 /opt/percona/bin/mysqld thp jemalloc53 noskip 110 test1 nobinlog innodb"
    exit 1
fi

SERVER_BINARY="$1"
THP_ENABLED="$2"
ALLOCATOR="$3"
SKIP_INIT="$4"
BUFFER_POOL_SIZE_GB="$5"
RESULTS_SUFFIX="$6"
ENABLE_BINLOG="$7"
STORAGE_ENGINE="$8"

# Set results directory with suffix and parameters
RESULTS_DIR="${SCRIPT_DIR}/results-${RESULTS_SUFFIX}-${THP_ENABLED}-${ALLOCATOR}-${BUFFER_POOL_SIZE_GB}G-${ENABLE_BINLOG}-${STORAGE_ENGINE}"

# Validate inputs
if [ ! -f "${SERVER_BINARY}" ]; then
    log_error "Server binary not found: ${SERVER_BINARY}"
    exit 1
fi

if [[ ! "${THP_ENABLED}" =~ ^(thp|nothp)$ ]]; then
    log_error "THP parameter must be 'thp' or 'nothp', got: ${THP_ENABLED}"
    exit 1
fi

if [[ ! "${ALLOCATOR}" =~ ^(jemalloc36|jemalloc53|tcmalloc|glibc)$ ]]; then
    log_error "Allocator must be one of: jemalloc36, jemalloc53, tcmalloc, glibc"
    exit 1
fi

if [[ ! "${SKIP_INIT}" =~ ^(skip|noskip)$ ]]; then
    log_error "Skip init parameter must be 'skip' or 'noskip', got: ${SKIP_INIT}"
    exit 1
fi

# Validate buffer pool size is a positive integer
if ! [[ "${BUFFER_POOL_SIZE_GB}" =~ ^[0-9]+$ ]] || [ "${BUFFER_POOL_SIZE_GB}" -lt 1 ]; then
    log_error "Buffer pool size must be a positive integer (in GB), got: ${BUFFER_POOL_SIZE_GB}"
    exit 1
fi

if [[ ! "${ENABLE_BINLOG}" =~ ^(binlog|nobinlog)$ ]]; then
    log_error "Enable binlog parameter must be 'binlog' or 'nobinlog', got: ${ENABLE_BINLOG}"
    exit 1
fi

if [[ ! "${STORAGE_ENGINE}" =~ ^(innodb|myrocks)$ ]]; then
    log_error "Storage engine parameter must be 'innodb' or 'myrocks', got: ${STORAGE_ENGINE}"
    exit 1
fi

# 1. Check if HammerDB 5.0 is installed
log_info "Checking HammerDB 5.0 installation..."
HAMMERDB_CLI=""

# First check current directory
if [ -f "${SCRIPT_DIR}/HammerDB-6.0/hammerdbcli" ]; then
    HAMMERDB_CLI="${SCRIPT_DIR}/HammerDB-6.0/hammerdbcli"
    log_info "Found HammerDB in current directory: ${HAMMERDB_CLI}"
else
    # Not in current directory, try to download
    log_info "HammerDB not found in current directory..."
    exit 1
fi

# Copy generic.xml to HammerDB config directory
# GENERIC_XML="${SCRIPT_DIR}/generic.xml"
# HAMMERDB_CONFIG_DIR="${SCRIPT_DIR}/HammerDB-5.0/config"

# if [ -f "${GENERIC_XML}" ]; then
#     log_info "Copying generic.xml to HammerDB config directory..."
#     mkdir -p "${HAMMERDB_CONFIG_DIR}"
#     cp "${GENERIC_XML}" "${HAMMERDB_CONFIG_DIR}/generic.xml"
#     log_info "generic.xml copied successfully"
# else
#     log_warn "generic.xml not found at ${GENERIC_XML}, skipping copy"
# fi

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
log_info "Storage engine: ${STORAGE_ENGINE}"
log_info "Binary logging: ${ENABLE_BINLOG}"

cat > "${MY_CNF}" <<EOF
[mysqld]
# Server configuration for HammerDB TPC-C benchmark

# Data directory
datadir=${SERVER_DATA_DIR}

EOF

# Add binary logging configuration based on parameter
if [ "${ENABLE_BINLOG}" = "binlog" ]; then
    cat >> "${MY_CNF}" <<EOF
# Binary logging enabled
log-bin = ${SERVER_DATA_DIR}/mysql-bin
sync_binlog = 1000
server_id = 1

EOF
else
    cat >> "${MY_CNF}" <<EOF
# Disable binary logging
skip-log-bin

EOF
fi

cat >> "${MY_CNF}" <<EOF
# Connection settings
max_connections = 200

# Logging
log-error = ${SERVER_DATA_DIR}/mysql-error.log
pid-file = ${SERVER_DATA_DIR}/mysql.pid

# Socket
socket = ${MYSQL_SOCKET}

# Disable SSL requirement
require_secure_transport = OFF

# Other settings
sql_mode = ""
wait_timeout = 288000        # 80 hours
interactive_timeout = 288000 # 80 hours
EOF

# Storage engine configuration
if [ "${STORAGE_ENGINE}" = "myrocks" ]; then
    log_info "Configuring MyRocks as default storage engine"
    log_info "RocksDB block cache size: ${BUFFER_POOL_SIZE_GB}G"
    cat >> "${MY_CNF}" <<EOF

# MyRocks storage engine
plugin-load=rocksdb=ha_rocksdb.so;rocksdb_cfstats=ha_rocksdb.so;rocksdb_dbstats=ha_rocksdb.so;rocksdb_perf_context=ha_rocksdb.so;rocksdb_perf_context_global=ha_rocksdb.so;rocksdb_cf_options=ha_rocksdb.so;rocksdb_compaction_stats=ha_rocksdb.so;rocksdb_global_info=ha_rocksdb.so;rocksdb_ddl=ha_rocksdb.so;rocksdb_index_file_map=ha_rocksdb.so;rocksdb_locks=ha_rocksdb.so;rocksdb_trx=ha_rocksdb.so
default-storage-engine = ROCKSDB
rocksdb_block_cache_size = ${BUFFER_POOL_SIZE_GB}G
rocksdb_max_open_files=-1
rocksdb_max_background_jobs=8
rocksdb_max_total_wal_size=4G
rocksdb_block_size=16384
rocksdb_table_cache_numshardbits=6

# rate limiter
rocksdb_bytes_per_sync=16777216
rocksdb_wal_bytes_per_sync=4194304

rocksdb_compaction_sequential_deletes_count_sd=1
rocksdb_compaction_sequential_deletes=199999
rocksdb_compaction_sequential_deletes_window=200000

rocksdb_default_cf_options="write_buffer_size=256m;target_file_size_base=32m;max_bytes_for_level_base=512m;max_write_buffer_number=4;level0_file_num_compaction_trigger=4;level0_slowdown_writes_trigger=20;level0_stop_writes_trigger=30;max_write_buffer_number=4;block_based_table_factory={cache_index_and_filter_blocks=1;filter_policy=bloomfilter:10:false;whole_key_filtering=0};level_compaction_dynamic_level_bytes=true;optimize_filters_for_hits=true;memtable_prefix_bloom_size_ratio=0.05;prefix_extractor=capped:12;compaction_pri=kMinOverlappingRatio;compression=kLZ4Compression;bottommost_compression=kLZ4Compression;compression_opts=-14:4:0"

rocksdb_max_subcompactions=4
rocksdb_compaction_readahead_size=16m

rocksdb_use_direct_reads=ON
rocksdb_use_direct_io_for_flush_and_compaction=ON
EOF
else
    log_info "InnoDB buffer pool size: ${BUFFER_POOL_SIZE_GB}G"
    cat >> "${MY_CNF}" <<EOF

# Table settings
default-storage-engine = InnoDB

# InnoDB redo log configuration
innodb_redo_log_capacity = 32G

# Minimize flush overhead (not crash-safe, but optimal for testing)
innodb_flush_log_at_trx_commit = 0

# Memory configuration
innodb_buffer_pool_size = ${BUFFER_POOL_SIZE_GB}G
innodb_buffer_pool_instances = 16
innodb_io_capacity = 20000

# Performance optimizations
innodb_flush_method = O_DIRECT
innodb_log_buffer_size = 256M
innodb_doublewrite = OFF
EOF
fi

# 5.1. Add large-pages=ON if THP is enabled
if [ "${THP_ENABLED}" = "thp" ]; then
    log_info "Transparent Huge Pages enabled - adding large-pages=ON to my.cnf"
    cat >> "${MY_CNF}" <<EOF

# Transparent Huge Pages
large-pages = ON
EOF
fi

log_info "Configuration file created successfully"

# 4. Remove old server data directory and create fresh one
if [ "${SKIP_INIT}" = "noskip" ]; then
    if [ -d "${SERVER_DATA_DIR}" ]; then
       log_info "Removing old server data directory: ${SERVER_DATA_DIR}"
       rm -rf "${SERVER_DATA_DIR}"
    fi

    log_info "Creating fresh server data directory: ${SERVER_DATA_DIR}"
    mkdir -p "${SERVER_DATA_DIR}"

    # Initialize data directory
    log_info "Initializing MySQL data directory..."
    if [ "${STORAGE_ENGINE}" = "myrocks" ]; then
        "${SERVER_BINARY}" --defaults-file="${MY_CNF}" --initialize-insecure --user=$(whoami) \
         --sync_binlog=0
    else
        "${SERVER_BINARY}" --defaults-file="${MY_CNF}" --initialize-insecure --user=$(whoami) \
         --innodb_flush_log_at_trx_commit=0 \
         --innodb_doublewrite=0 \
         --sync_binlog=0 \
         --innodb_buffer_pool_size=1G
    fi
else
    log_info "Skipping initialization - using existing data directory: ${SERVER_DATA_DIR}"
    if [ ! -d "${SERVER_DATA_DIR}" ]; then
        log_error "Data directory does not exist: ${SERVER_DATA_DIR}"
        exit 1
    fi
fi

# Set LD_PRELOAD for jemalloc if specified
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
elif [ "${ALLOCATOR}" = "jemalloc53" ]; then
    JEMALLOC_LIB="/usr/lib/x86_64-linux-gnu/libjemalloc.so.2"

    if [ -f "${JEMALLOC_LIB}" ]; then
        export LD_PRELOAD="${JEMALLOC_LIB}"
        log_info "LD_PRELOAD set to: ${LD_PRELOAD}"
    else
        log_error "jemalloc53 library not found at: ${JEMALLOC_LIB}"
        log_error "Install with: sudo apt-get install libjemalloc2"
        exit 1
    fi
elif [ "${ALLOCATOR}" = "tcmalloc" ]; then
    TCMALLOC_LIB="/usr/lib/x86_64-linux-gnu/libtcmalloc.so.4"

    if [ -f "${TCMALLOC_LIB}" ]; then
        export LD_PRELOAD="${TCMALLOC_LIB}"
        log_info "LD_PRELOAD set to: ${LD_PRELOAD}"
    else
        log_error "tcmalloc library not found at: ${TCMALLOC_LIB}"
        log_error "Install with: sudo apt-get install libgoogle-perftools-dev"
        exit 1
    fi
fi

# Start MySQL server
log_info "Starting MySQL server..."
log_info "Command: ${SERVER_BINARY} --defaults-file=${MY_CNF} --user=$(whoami)"
"${SERVER_BINARY}" --defaults-file="${MY_CNF}" --user=$(whoami) &
MYSQLD_PID=$!

# Set OOM score adjustment to protect mysqld from OOM killer
# log_info "Setting OOM score adjustment to -500 for mysqld (PID: ${MYSQLD_PID})..."
# echo -500 | sudo tee /proc/${MYSQLD_PID}/oom_score_adj > /dev/null || log_warn "Failed to set OOM score adjustment"

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

    # Check if mysqld process is still alive
    if ! kill -0 ${MYSQLD_PID} 2>/dev/null; then
        log_error "mysqld process (PID: ${MYSQLD_PID}) has died"
        log_error "Check error log: ${SERVER_DATA_DIR}/mysql-error.log"
        exit 1
    fi

    set +e
    CONNECT_OUTPUT=$("${MYSQL_CLIENT}" --socket="${MYSQL_SOCKET}" -u root -e "SELECT 1" 2>&1)
    MYSQL_EXIT_CODE=$?
    set -e
    log_info "OUT=$CONNECT_OUTPUT"
    if [ $MYSQL_EXIT_CODE -eq 0 ]; then
        log_info "MySQL server is ready"
        break
    fi
    if [ $i -eq 300 ]; then
        log_error "MySQL server failed to start after 600 seconds"
        log_error "Last connection error: ${CONNECT_OUTPUT}"
        log_error "Check error log: ${SERVER_DATA_DIR}/mysql-error.log"
        kill ${MYSQLD_PID} 2>/dev/null || true
        exit 1
    fi
    log_warn "Connection attempt $i/300 failed, retrying... (${CONNECT_OUTPUT})"
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
            if grep -q "libjemalloc.so.2" /proc/${pid}/maps; then
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
if [ "${SKIP_INIT}" = "noskip" ]; then
    log_info "Building TPC-C database schema using HammerDB..."
    log_info "This may take a while..."

    "${HAMMERDB_CLI}" auto "${HAMMERDB_LOAD_TCL}" || {
       log_error "Database build failed"
       kill ${MYSQLD_PID} 2>/dev/null || true
       exit 1
    }

    log_info "Database build completed successfully"
else
    log_info "Skipping database build - using existing database"
fi

# Create results directory
mkdir -p "${RESULTS_DIR}"

# Create HammerDB run script for TPC-C test
HAMMERDB_RUN_TCL="${SCRIPT_DIR}/hammerdb_run.tcl"

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
diset tpcc mysql_rampup ${RAMPUP_DURATION_MINUTES}
diset tpcc mysql_duration ${BENCHMARK_DURATION_MINUTES}
diset tpcc mysql_allwarehouse true
diset tpcc mysql_timeprofile true
diset tpcc mysql_history_pk true
diset tpcc mysql_total_iterations 100000000

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

# 8. Run TPC-C test with configured Virtual Users and duration
TOTAL_DURATION_MINUTES=$((BENCHMARK_DURATION_MINUTES + RAMPUP_DURATION_MINUTES))
BENCHMARK_DURATION_HOURS=$((BENCHMARK_DURATION_MINUTES / 60))
TOTAL_DURATION_HOURS=$((TOTAL_DURATION_MINUTES / 60))
log_info "Starting TPC-C benchmark: ${VIRTUAL_USERS} VUs"
log_info "  Ramp-up: ${RAMPUP_DURATION_MINUTES} minutes"
log_info "  Benchmark: ${BENCHMARK_DURATION_MINUTES} minutes (${BENCHMARK_DURATION_HOURS} hours)"
log_info "  Total duration: ${TOTAL_DURATION_MINUTES} minutes (${TOTAL_DURATION_HOURS} hours)"

# Start HammerDB in background
HAMMERDB_OUTPUT_FILE="${RESULTS_DIR}/${THP_ENABLED}_${ALLOCATOR}_hammerdb_output.log"
"${HAMMERDB_CLI}" auto "${HAMMERDB_RUN_TCL}" > "${HAMMERDB_OUTPUT_FILE}" 2>&1 &
HAMMERDB_PID=$!

# Set up trap to gracefully stop HammerDB on script termination
trap_handler() {
    log_warn "Received termination signal, stopping benchmark gracefully..."
    if kill -0 ${HAMMERDB_PID} 2>/dev/null; then
        log_info "Sending SIGINT to HammerDB (PID: ${HAMMERDB_PID}) to generate summary..."
        kill -INT ${HAMMERDB_PID} 2>/dev/null || true
        log_info "Waiting for HammerDB to finish writing summary..."
        wait ${HAMMERDB_PID} 2>/dev/null || true
    fi

    # Stop data collectors
    [ -n "${COLLECTOR_PID}" ] && kill ${COLLECTOR_PID} 2>/dev/null || true
    [ -n "${RSS_COLLECTOR_PID}" ] && kill ${RSS_COLLECTOR_PID} 2>/dev/null || true
    [ -n "${MYSQL_GLOBALS_PID}" ] && kill ${MYSQL_GLOBALS_PID} 2>/dev/null || true
    [ -n "${VMSTAT_PID}" ] && kill ${VMSTAT_PID} 2>/dev/null || true

    # Stop MySQL
    if kill -0 ${MYSQLD_PID} 2>/dev/null; then
        log_info "Stopping MySQL server..."
        kill ${MYSQLD_PID} 2>/dev/null || true
        wait ${MYSQLD_PID} 2>/dev/null || true
    fi

    log_info "Cleanup completed"
    exit 130
}

trap trap_handler INT TERM

# Set OOM score adjustment to protect hammerdbcli from OOM killer
# log_info "Setting OOM score adjustment to -500 for hammerdbcli (PID: ${HAMMERDB_PID})..."
# echo -500 | sudo tee /proc/${HAMMERDB_PID}/oom_score_adj > /dev/null || log_warn "Failed to set OOM score adjustment for hammerdbcli"

# 9. Print remaining time every 10 seconds
# 10. Collect /proc/<pid>/status every 1 second
# 11. Collect /proc/<pid>/smaps_rollup every 1 second

log_info "Benchmark running (HammerDB PID: ${HAMMERDB_PID}, MySQL PID: ${MYSQLD_PID})"
log_info "Results directory: ${RESULTS_DIR}"

START_TIME=$(date +%s)
END_TIME=$((START_TIME + TOTAL_DURATION_MINUTES * 60))

DATE_TIME=$(date +%Y%m%d_%H%M%S)
FILE_PREFIX="${THP_ENABLED}_${ALLOCATOR}"
STATUS_FILE="${RESULTS_DIR}/${FILE_PREFIX}_mysql_status_${DATE_TIME}.log"
SMAPS_ROLLUP_FILE="${RESULTS_DIR}/${FILE_PREFIX}_mysql_smaps_rollup_${DATE_TIME}.log"
SMAPS_FILE="${RESULTS_DIR}/${FILE_PREFIX}_mysql_smaps_${DATE_TIME}.log"
STAT_FILE="${RESULTS_DIR}/${FILE_PREFIX}_mysql_stat_${DATE_TIME}.log"
MAPS_FILE="${RESULTS_DIR}/${FILE_PREFIX}_mysql_maps_${DATE_TIME}.log"
RSS_FILE="${RESULTS_DIR}/${FILE_PREFIX}_rss_memory_${DATE_TIME}.log"
GLOBAL_STATUS_FILE="${RESULTS_DIR}/${FILE_PREFIX}_global_status_${DATE_TIME}.log"
GLOBAL_VARS_FILE="${RESULTS_DIR}/${FILE_PREFIX}_global_vars_${DATE_TIME}.log"
VMSTAT_FILE="${RESULTS_DIR}/${FILE_PREFIX}_vmstat_${DATE_TIME}.log"

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

echo "# RSS (Resident Memory Size) monitoring for mysqld and hammerdbcli" > "${RSS_FILE}"
echo "# Started at: $(date)" >> "${RSS_FILE}"
echo "# Format: Timestamp, mysqld_PID, mysqld_RSS_KB, hammerdbcli_PID, hammerdbcli_RSS_KB" >> "${RSS_FILE}"
echo "" >> "${RSS_FILE}"

echo "# MySQL SHOW GLOBAL STATUS data collection (every 30 seconds)" > "${GLOBAL_STATUS_FILE}"
echo "# Started at: $(date)" >> "${GLOBAL_STATUS_FILE}"
echo "" >> "${GLOBAL_STATUS_FILE}"

echo "# MySQL SHOW GLOBAL VARIABLES data collection (every 30 seconds)" > "${GLOBAL_VARS_FILE}"
echo "# Started at: $(date)" >> "${GLOBAL_VARS_FILE}"
echo "" >> "${GLOBAL_VARS_FILE}"

echo "# vmstat system statistics (every 1 second)" > "${VMSTAT_FILE}"
echo "# Started at: $(date)" >> "${VMSTAT_FILE}"
echo "" >> "${VMSTAT_FILE}"

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

# RSS monitoring function
collect_rss_data() {
    local mysqld_pid=$1
    local hammerdb_pid=$2
    local rss_file=$3

    while kill -0 ${mysqld_pid} 2>/dev/null && kill -0 ${hammerdb_pid} 2>/dev/null; do
        TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

        # Get RSS for mysqld (in KB)
        MYSQLD_RSS=0
        if [ -f "/proc/${mysqld_pid}/status" ]; then
            MYSQLD_RSS=$(grep "^VmRSS:" /proc/${mysqld_pid}/status 2>/dev/null | awk '{print $2}')
            [ -z "${MYSQLD_RSS}" ] && MYSQLD_RSS=0
        fi

        # Get RSS for hammerdbcli (in KB)
        HAMMERDB_RSS=0
        if [ -f "/proc/${hammerdb_pid}/status" ]; then
            HAMMERDB_RSS=$(grep "^VmRSS:" /proc/${hammerdb_pid}/status 2>/dev/null | awk '{print $2}')
            [ -z "${HAMMERDB_RSS}" ] && HAMMERDB_RSS=0
        fi

        # Write to log file in CSV format
        echo "${TIMESTAMP}, ${mysqld_pid}, ${MYSQLD_RSS}, ${hammerdb_pid}, ${HAMMERDB_RSS}" >> "${rss_file}"

        # Check if combined RSS exceeds 180GB  (188743680 KB)
        COMBINED_RSS=$((MYSQLD_RSS + HAMMERDB_RSS))
        RSS_LIMIT_KB=188743680  # 180 GB in KB

        if [ ${COMBINED_RSS} -gt ${RSS_LIMIT_KB} ]; then
            COMBINED_RSS_GB=$((COMBINED_RSS / 1024 / 1024))
            log_error "Combined RSS (${COMBINED_RSS_GB} GB) exceeded limit of 180 GB!"
            log_error "mysqld RSS: $((MYSQLD_RSS / 1024 / 1024)) GB, hammerdbcli RSS: $((HAMMERDB_RSS / 1024 / 1024)) GB"
            log_error "Terminating benchmark due to memory limit exceeded"

            # Gracefully stop HammerDB first (allow it to write summary)
            log_info "Sending graceful stop signal (SIGINT) to HammerDB..."
            kill -INT ${hammerdb_pid} 2>/dev/null || true
            sleep 5  # Give HammerDB time to write summary and profile

            # Then stop other processes
            kill ${mysqld_pid} 2>/dev/null || true
            [ -n "${COLLECTOR_PID}" ] && kill ${COLLECTOR_PID} 2>/dev/null || true
            [ -n "${MYSQL_GLOBALS_PID}" ] && kill ${MYSQL_GLOBALS_PID} 2>/dev/null || true
            [ -n "${VMSTAT_PID}" ] && kill ${VMSTAT_PID} 2>/dev/null || true

            exit 1
        fi

        sleep 1
    done
}

# MySQL global status and variables monitoring function
collect_mysql_globals() {
    local mysql_client=$1
    local mysql_socket=$2
    local global_status_file=$3
    local global_vars_file=$4
    local mysqld_pid=$5
    local hammerdb_pid=$6

    local iteration=0

    while kill -0 ${mysqld_pid} 2>/dev/null && kill -0 ${hammerdb_pid} 2>/dev/null; do
        # Collect every 30 seconds
        if [ $((iteration % 30)) -eq 0 ]; then
            TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")

            # Collect SHOW GLOBAL STATUS
            echo "=== ${TIMESTAMP} ===" >> "${global_status_file}"
            "${mysql_client}" --socket="${mysql_socket}" -u root -e "SHOW GLOBAL STATUS;" >> "${global_status_file}" 2>/dev/null || true
            echo "" >> "${global_status_file}"

            # Collect SHOW GLOBAL VARIABLES
            echo "=== ${TIMESTAMP} ===" >> "${global_vars_file}"
            "${mysql_client}" --socket="${mysql_socket}" -u root -e "SHOW GLOBAL VARIABLES;" >> "${global_vars_file}" 2>/dev/null || true
            echo "" >> "${global_vars_file}"
        fi

        iteration=$((iteration + 1))
        sleep 1
    done
}

# vmstat system statistics monitoring function
collect_vmstat() {
    local vmstat_file=$1
    local mysqld_pid=$2
    local hammerdb_pid=$3

    # Run vmstat 1 to get output every 1 second
    # The first line will be averages since boot, then real-time data
    vmstat -t 1 > "${vmstat_file}" 2>&1 &
    local vmstat_pid=$!

    # Monitor and kill vmstat when benchmark stops
    while kill -0 ${mysqld_pid} 2>/dev/null && kill -0 ${hammerdb_pid} 2>/dev/null; do
        sleep 5
    done

    # Kill vmstat when benchmark is done
    kill ${vmstat_pid} 2>/dev/null || true
}

# Initialize background process PIDs
COLLECTOR_PID=""
RSS_COLLECTOR_PID=""
MYSQL_GLOBALS_PID=""
VMSTAT_PID=""

# Start data collection in background
collect_proc_data ${MYSQLD_PID} "${STATUS_FILE}" "${SMAPS_ROLLUP_FILE}" "${SMAPS_FILE}" "${STAT_FILE}" "${MAPS_FILE}" &
COLLECTOR_PID=$!

# Start RSS monitoring in background
collect_rss_data ${MYSQLD_PID} ${HAMMERDB_PID} "${RSS_FILE}" &
RSS_COLLECTOR_PID=$!

# Start MySQL global status/variables monitoring in background
collect_mysql_globals "${MYSQL_CLIENT}" "${MYSQL_SOCKET}" "${GLOBAL_STATUS_FILE}" "${GLOBAL_VARS_FILE}" ${MYSQLD_PID} ${HAMMERDB_PID} &
MYSQL_GLOBALS_PID=$!

# Start vmstat monitoring in background
collect_vmstat "${VMSTAT_FILE}" ${MYSQLD_PID} ${HAMMERDB_PID} &
VMSTAT_PID=$!

# Time reporting loop
LAST_REPORT=0
while kill -0 ${HAMMERDB_PID} 2>/dev/null; do
    CURRENT_TIME=$(date +%s)
    ELAPSED=$((CURRENT_TIME - START_TIME))
    REMAINING=$((END_TIME - CURRENT_TIME))

    if [ $REMAINING -lt 0 ]; then
        REMAINING=0
    fi

    # Check if mysqld process is still alive
    if ! kill -0 ${MYSQLD_PID} 2>/dev/null; then
        log_error "mysqld process (PID: ${MYSQLD_PID}) has died unexpectedly!"
        log_error "Check error log: ${SERVER_DATA_DIR}/mysql-error.log"
        kill ${HAMMERDB_PID} 2>/dev/null || true
        [ -n "${COLLECTOR_PID}" ] && kill ${COLLECTOR_PID} 2>/dev/null || true
        [ -n "${RSS_COLLECTOR_PID}" ] && kill ${RSS_COLLECTOR_PID} 2>/dev/null || true
        [ -n "${MYSQL_GLOBALS_PID}" ] && kill ${MYSQL_GLOBALS_PID} 2>/dev/null || true
        [ -n "${VMSTAT_PID}" ] && kill ${VMSTAT_PID} 2>/dev/null || true
        exit 1
    fi

    # Check if hammerdbcli process is still alive
    if ! kill -0 ${HAMMERDB_PID} 2>/dev/null; then
        log_error "hammerdbcli process (PID: ${HAMMERDB_PID}) has died unexpectedly!"
        break
    fi

    # Report every 10 seconds
    if [ $((ELAPSED - LAST_REPORT)) -ge 10 ]; then
        HOURS=$((REMAINING / 3600))
        MINUTES=$(((REMAINING % 3600) / 60))
        SECONDS=$((REMAINING % 60))

        log_info "Benchmark progress (${RESULTS_SUFFIX}) - Time remaining: ${HOURS}h ${MINUTES}m ${SECONDS}s"
        LAST_REPORT=$ELAPSED
    fi

    # Gracefully stop HammerDB after 3 minutes to simulate unexpected stop
    # if [ $ELAPSED -ge 180 ]; then
    #     log_warn "Simulating unexpected stop: 3 minutes elapsed, stopping HammerDB..."
    #     if kill -0 ${HAMMERDB_PID} 2>/dev/null; then
    #         log_info "Stopping HammerDB and all its child processes..."
    #         # Kill the entire process group to ensure all HammerDB processes are terminated
    #         pkill -TERM -P ${HAMMERDB_PID} 2>/dev/null || true
    #         kill -TERM ${HAMMERDB_PID} 2>/dev/null || true
    #         sleep 10
    #         # Force kill if still running
    #         pkill -KILL -P ${HAMMERDB_PID} 2>/dev/null || true
    #         kill -KILL ${HAMMERDB_PID} 2>/dev/null || true
    #         log_info "HammerDB stopped"
    #         break
    #     fi
    # fi

    sleep 1
done

# Wait for HammerDB to complete
wait ${HAMMERDB_PID}
HAMMERDB_EXIT=$?

# Stop data collection
[ -n "${COLLECTOR_PID}" ] && kill ${COLLECTOR_PID} 2>/dev/null || true
[ -n "${COLLECTOR_PID}" ] && wait ${COLLECTOR_PID} 2>/dev/null || true

[ -n "${RSS_COLLECTOR_PID}" ] && kill ${RSS_COLLECTOR_PID} 2>/dev/null || true
[ -n "${RSS_COLLECTOR_PID}" ] && wait ${RSS_COLLECTOR_PID} 2>/dev/null || true

[ -n "${MYSQL_GLOBALS_PID}" ] && kill ${MYSQL_GLOBALS_PID} 2>/dev/null || true
[ -n "${MYSQL_GLOBALS_PID}" ] && wait ${MYSQL_GLOBALS_PID} 2>/dev/null || true

[ -n "${VMSTAT_PID}" ] && kill ${VMSTAT_PID} 2>/dev/null || true
[ -n "${VMSTAT_PID}" ] && wait ${VMSTAT_PID} 2>/dev/null || true

log_info "Benchmark completed (exit code: ${HAMMERDB_EXIT})"

# Stop MySQL server
log_info "Stopping MySQL server (PID: ${MYSQLD_PID})..."
if kill -0 ${MYSQLD_PID} 2>/dev/null; then
    kill -9 ${MYSQLD_PID} 2>/dev/null || true
    log_info "MySQL server killed"
else
    log_info "MySQL server already stopped"
fi

# Copy HammerDB transaction profile log if it exists
HDBXTPROFILE_SRC="/tmp/hdbxtprofile.log"
if [ -f "${HDBXTPROFILE_SRC}" ]; then
    HDBXTPROFILE_DEST="${RESULTS_DIR}/${THP_ENABLED}_${ALLOCATOR}_hdbxtprofile.log"
    log_info "Copying HammerDB transaction profile log..."
    cp "${HDBXTPROFILE_SRC}" "${HDBXTPROFILE_DEST}"
    log_info "  - HammerDB profile: ${HDBXTPROFILE_DEST}"
else
    log_warn "HammerDB transaction profile log not found at: ${HDBXTPROFILE_SRC}"
fi

# Summary
log_info "======================================"
log_info "Benchmark Summary"
log_info "======================================"
log_info "Server binary: ${SERVER_BINARY}"
log_info "Allocator: ${ALLOCATOR}"
log_info "THP enabled: ${THP_ENABLED}"
log_info "Buffer pool size: ${BUFFER_POOL_SIZE_GB}G"
log_info "Binary logging: ${ENABLE_BINLOG}"
log_info "Storage engine: ${STORAGE_ENGINE}"
log_info "Results suffix: ${RESULTS_SUFFIX}"
log_info "Virtual Users: ${VIRTUAL_USERS}"
log_info "Ramp-up duration: ${RAMPUP_DURATION_MINUTES} minutes"
log_info "Benchmark duration: ${BENCHMARK_DURATION_MINUTES} minutes (${BENCHMARK_DURATION_HOURS} hours)"
log_info "Total duration: ${TOTAL_DURATION_MINUTES} minutes (${TOTAL_DURATION_HOURS} hours)"
log_info "Results directory: ${RESULTS_DIR}"
log_info "  - HammerDB output: ${HAMMERDB_OUTPUT_FILE}"
log_info "  - MySQL status data: ${STATUS_FILE}"
log_info "  - MySQL smaps_rollup data: ${SMAPS_ROLLUP_FILE}"
log_info "  - MySQL smaps data: ${SMAPS_FILE}"
log_info "  - MySQL stat data: ${STAT_FILE}"
log_info "  - MySQL maps data: ${MAPS_FILE}"
log_info "  - RSS memory data: ${RSS_FILE}"
log_info "  - MySQL global status data: ${GLOBAL_STATUS_FILE}"
log_info "  - MySQL global variables data: ${GLOBAL_VARS_FILE}"
log_info "  - vmstat system statistics: ${VMSTAT_FILE}"
if [ -f "${RESULTS_DIR}/${THP_ENABLED}_${ALLOCATOR}_hdbxtprofile.log" ]; then
    log_info "  - HammerDB transaction profile: ${RESULTS_DIR}/${THP_ENABLED}_${ALLOCATOR}_hdbxtprofile.log"
fi
log_info "======================================"

exit ${HAMMERDB_EXIT}
