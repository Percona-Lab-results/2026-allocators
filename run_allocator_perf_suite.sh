#!/bin/bash
#
# Allocator performance test suite.
#
# Implements a phased benchmark design that stresses the memory allocator
# itself (not just a huge stable buffer pool):
#
#   - deliberately small buffer pool (default 48G) so per-query / per-connection
#     allocations are a meaningful share of RSS
#   - malloc-heavy stress clients (big sorts, GROUP_CONCAT, temp tables,
#     prepared-statement churn) with connect/disconnect cycling
#   - phases per run:  steady load -> idle (RSS release-to-OS) -> regrow
#     (fragmentation ratchet), all within one mysqld lifetime
#   - N repetitions per allocator with a fresh mysqld per repetition
#   - in-run verification: allocator .so actually loaded, kernel THP setting
#     applied, and (for --thp=yes) AnonHugePages > 0 once warmed up
#   - collectors: smaps_rollup + RSS every 5s, maps every 30s, smaps every
#     60s, performance_schema tracked-memory (live-bytes proxy) every 30s,
#     phase markers, per-phase NOPM
#
# Results use the same file naming as run_hammerdb_benchmark.sh
# (<thp>_<allocator>_mysql_maps_<ts>.log etc.), so generate_maps_report.py /
# generate_smaps_report.py / generate_rss_report.py work on the output.
# A machine-readable suite_summary.csv aggregates NOPM and RSS per phase.
#
# Usage:
#   ./run_allocator_perf_suite.sh --server=/path/to/mysqld \
#       --allocators=glibc,jemalloc36,jemalloc53,tcmalloc \
#       --thp=yes|no --reps=3 --buffer-gb=48 --results-suffix=allocperf \
#       [--skip-init=yes|no] [--snapshot=yes|no] [--vu=80] [--stress-clients=8] \
#       [--steady-minutes=60] [--idle-minutes=30] [--regrow-minutes=60] \
#       [--rampup-minutes=10] [--max-freq=2400]
#
# Example (3 reps x 3 allocators, ~9 x 2.7h = ~24h total):
#   ./run_allocator_perf_suite.sh --server=/mnt/nvme/servers/PS-8.4.10/bin/mysqld \
#       --allocators=glibc,jemalloc53,tcmalloc --thp=no --reps=3 \
#       --buffer-gb=48 --results-suffix=allocperf1 --skip-init=no --snapshot=yes

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DATA_DIR="${HOME}/servers/data"
SNAPSHOT_DIR="${HOME}/servers/data-snapshot"
MY_CNF="${SCRIPT_DIR}/my-allocsuite.cnf"
HAMMERDB_LOAD_TCL="${SCRIPT_DIR}/hammerdb_load.tcl"
MYSQL_SOCKET="/tmp/mysql-alloc-test.sock"
THP_SYSFS="/sys/kernel/mm/transparent_hugepage/enabled"
THP_RESTORE_VALUE="madvise"

# Defaults (overridable via arguments)
ALLOCATORS="glibc,jemalloc53,tcmalloc"
THP_MODE="no"
REPS=3
BUFFER_POOL_SIZE_GB=48
VIRTUAL_USERS=80
STRESS_CLIENTS=8
STEADY_MINUTES=60
IDLE_MINUTES=30
REGROW_MINUTES=60
RAMPUP_MINUTES=10
CPU_MAX_FREQ_MHZ=2400
SKIP_INIT="skip"
USE_SNAPSHOT="no"
RESULTS_SUFFIX=""
SERVER_BINARY=""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} [$(date +"%Y-%m-%d %H:%M:%S")] $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} [$(date +"%Y-%m-%d %H:%M:%S")] $1" >&2; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} [$(date +"%Y-%m-%d %H:%M:%S")] $1"; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
for arg in "$@"; do
    case $arg in
        --server=*)          SERVER_BINARY="${arg#*=}" ;;
        --allocators=*)      ALLOCATORS="${arg#*=}" ;;
        --thp=*)             THP_MODE="${arg#*=}" ;;
        --reps=*)            REPS="${arg#*=}" ;;
        --buffer-gb=*)       BUFFER_POOL_SIZE_GB="${arg#*=}" ;;
        --vu=*)              VIRTUAL_USERS="${arg#*=}" ;;
        --stress-clients=*)  STRESS_CLIENTS="${arg#*=}" ;;
        --steady-minutes=*)  STEADY_MINUTES="${arg#*=}" ;;
        --idle-minutes=*)    IDLE_MINUTES="${arg#*=}" ;;
        --regrow-minutes=*)  REGROW_MINUTES="${arg#*=}" ;;
        --rampup-minutes=*)  RAMPUP_MINUTES="${arg#*=}" ;;
        --max-freq=*)        CPU_MAX_FREQ_MHZ="${arg#*=}" ;;
        --results-suffix=*)  RESULTS_SUFFIX="${arg#*=}" ;;
        --skip-init=*)
            case "${arg#*=}" in
                yes) SKIP_INIT="skip" ;;
                no)  SKIP_INIT="noskip" ;;
                *) log_error "Invalid --skip-init (yes|no)"; exit 1 ;;
            esac ;;
        --snapshot=*)        USE_SNAPSHOT="${arg#*=}" ;;
        *) log_error "Unknown argument: $arg"; exit 1 ;;
    esac
done

if [ -z "${SERVER_BINARY}" ] || [ -z "${RESULTS_SUFFIX}" ]; then
    log_error "Required: --server=/path/to/mysqld --results-suffix=<name>"
    exit 1
fi
if [ ! -x "${SERVER_BINARY}" ]; then
    log_error "Server binary not found or not executable: ${SERVER_BINARY}"
    exit 1
fi
if [[ ! "${THP_MODE}" =~ ^(yes|no)$ ]]; then
    log_error "--thp must be yes or no"
    exit 1
fi
IFS=',' read -ra ALLOCATOR_LIST <<< "${ALLOCATORS}"
for a in "${ALLOCATOR_LIST[@]}"; do
    if [[ ! "$a" =~ ^(glibc|jemalloc36|jemalloc53|tcmalloc)$ ]]; then
        log_error "Unknown allocator: $a (glibc|jemalloc36|jemalloc53|tcmalloc)"
        exit 1
    fi
done

THP_ENABLED="nothp"; [ "${THP_MODE}" = "yes" ] && THP_ENABLED="thp"
MYSQL_CLIENT="$(dirname "${SERVER_BINARY}")/mysql"
HAMMERDB_CLI="${SCRIPT_DIR}/HammerDB-6.0/hammerdbcli"
SUITE_DIR="${SCRIPT_DIR}/suite-${RESULTS_SUFFIX}"
SUMMARY_CSV="${SUITE_DIR}/suite_summary.csv"

[ -f "${HAMMERDB_CLI}" ] || { log_error "HammerDB not found: ${HAMMERDB_CLI}"; exit 1; }
[ -f "${MYSQL_CLIENT}" ] || { log_error "mysql client not found: ${MYSQL_CLIENT}"; exit 1; }
[ -f "${SCRIPT_DIR}/mysqloltp.tcl" ] || { log_error "mysqloltp.tcl not found in ${SCRIPT_DIR}"; exit 1; }

mkdir -p "${SUITE_DIR}"
if [ ! -f "${SUMMARY_CSV}" ]; then
    echo "allocator,thp,rep,phase,start,end,nopm,tpm,avg_rss_mb,max_rss_mb,end_rss_mb,avg_tracked_mb" > "${SUMMARY_CSV}"
fi

ulimit -n 65536

# ---------------------------------------------------------------------------
# Global cleanup: everything spawned by the current run
# ---------------------------------------------------------------------------
MYSQLD_PID=""
HAMMERDB_PID=""
COLLECTOR_PID=""
STRESS_PIDS=()

stop_stress_clients() {
    for p in "${STRESS_PIDS[@]:-}"; do
        [ -n "$p" ] && kill "$p" 2>/dev/null || true
    done
    STRESS_PIDS=()
    # Kill any stress mysql clients still connecting
    pkill -f "allocator-stress-marker" 2>/dev/null || true
}

restore_thp() {
    log_info "Restoring THP setting to '${THP_RESTORE_VALUE}'..."
    echo "${THP_RESTORE_VALUE}" | sudo tee "${THP_SYSFS}" > /dev/null 2>&1 || \
        log_warn "Failed to restore THP setting"
}

cleanup_run() {
    [ -n "${HAMMERDB_PID}" ] && kill -9 "${HAMMERDB_PID}" 2>/dev/null || true
    stop_stress_clients
    [ -n "${COLLECTOR_PID}" ] && kill "${COLLECTOR_PID}" 2>/dev/null || true
    if [ -n "${MYSQLD_PID}" ] && kill -0 "${MYSQLD_PID}" 2>/dev/null; then
        log_info "Stopping mysqld (PID ${MYSQLD_PID})..."
        kill "${MYSQLD_PID}" 2>/dev/null || true
        for i in {1..120}; do
            kill -0 "${MYSQLD_PID}" 2>/dev/null || break
            sleep 1
        done
        kill -9 "${MYSQLD_PID}" 2>/dev/null || true
    fi
    MYSQLD_PID=""; HAMMERDB_PID=""; COLLECTOR_PID=""
}

trap_handler() {
    log_warn "Termination signal received, cleaning up..."
    cleanup_run
    restore_thp
    exit 130
}
trap trap_handler INT TERM
trap restore_thp EXIT

# ---------------------------------------------------------------------------
# Kernel THP configuration (sysfs; never large-pages in my.cnf)
# ---------------------------------------------------------------------------
set_thp() {
    local value=$1
    echo "${value}" | sudo tee "${THP_SYSFS}" > /dev/null || {
        log_error "Failed to set THP to '${value}' (sudo write to ${THP_SYSFS})"
        exit 1
    }
    local active
    active=$(cat "${THP_SYSFS}")
    log_info "Kernel THP setting: ${active}"
    if [[ "${active}" != *"[${value}]"* ]]; then
        log_error "THP setting did not take effect (wanted '${value}', got '${active}')"
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# CPU frequency pinning (same as main harness, best effort)
# ---------------------------------------------------------------------------
setup_cpu() {
    sudo cpupower frequency-set -g performance 2>/dev/null || \
        log_warn "Could not set CPU governor"
    if ! sudo cpupower frequency-set -u "${CPU_MAX_FREQ_MHZ}MHz" > /dev/null 2>&1; then
        for max_freq in /sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq; do
            [ -f "$max_freq" ] && echo $((CPU_MAX_FREQ_MHZ * 1000)) | sudo tee "$max_freq" > /dev/null 2>&1 || true
        done
    fi
    for cpu_idle in /sys/devices/system/cpu/cpu*/cpuidle/state*/disable; do
        [ -f "$cpu_idle" ] && echo 1 | sudo tee "$cpu_idle" > /dev/null 2>&1 || true
    done
}

# ---------------------------------------------------------------------------
# my.cnf generation (small buffer pool by design; no binlog; InnoDB)
# ---------------------------------------------------------------------------
write_my_cnf() {
    cat > "${MY_CNF}" <<EOF
[mysqld]
# Allocator performance suite configuration
datadir=${SERVER_DATA_DIR}
socket=${MYSQL_SOCKET}
log-error=${SERVER_DATA_DIR}/mysql-error.log
pid-file=${SERVER_DATA_DIR}/mysqld.pid

max_connections = 2000
back_log = 1500

skip-log-bin
innodb_flush_log_at_trx_commit = 0
innodb_flush_method = O_DIRECT
innodb_log_buffer_size = 256M
innodb_doublewrite = OFF
innodb_io_capacity = 20000

# Deliberately small buffer pool: allocator behavior, not the buffer pool,
# should dominate per-query memory traffic.
innodb_buffer_pool_size = ${BUFFER_POOL_SIZE_GB}G
innodb_buffer_pool_instances = 8

# performance_schema memory instrumentation = allocator-independent
# "live bytes" reference for RSS overhead calculations
performance_schema = ON
EOF
    log_info "Wrote ${MY_CNF} (buffer pool ${BUFFER_POOL_SIZE_GB}G)"
}

# ---------------------------------------------------------------------------
# Allocator preload selection and verification
# ---------------------------------------------------------------------------
allocator_preload() {
    local allocator=$1
    case "${allocator}" in
        jemalloc36)
            local server_dir
            server_dir=$(dirname "$(dirname "${SERVER_BINARY}")")
            echo "${server_dir}/lib/mysql/libjemalloc.so.1" ;;
        jemalloc53) echo "/usr/lib/x86_64-linux-gnu/libjemalloc.so.2" ;;
        tcmalloc)   echo "/usr/lib/x86_64-linux-gnu/libtcmalloc.so.4" ;;
        glibc)      echo "" ;;
    esac
}

check_allocator_loaded() {
    local pid=$1 allocator=$2
    case "${allocator}" in
        jemalloc36) grep -q "libjemalloc\.so\.1" "/proc/${pid}/maps" ;;
        jemalloc53) grep -q "libjemalloc\.so\.2" "/proc/${pid}/maps" ;;
        tcmalloc)   grep -q "libtcmalloc"        "/proc/${pid}/maps" ;;
        glibc)      ! grep -q -E "libjemalloc|libtcmalloc" "/proc/${pid}/maps" ;;
    esac
}

# ---------------------------------------------------------------------------
# mysqld lifecycle
# ---------------------------------------------------------------------------
start_mysqld() {
    local allocator=$1
    local preload
    preload=$(allocator_preload "${allocator}")

    if [ -n "${preload}" ]; then
        if [ ! -f "${preload}" ]; then
            log_error "Allocator library not found: ${preload}"
            return 1
        fi
        log_info "Starting mysqld with LD_PRELOAD=${preload}"
        LD_PRELOAD="${preload}" "${SERVER_BINARY}" --defaults-file="${MY_CNF}" --user=$(whoami) &
    else
        log_info "Starting mysqld with glibc malloc"
        "${SERVER_BINARY}" --defaults-file="${MY_CNF}" --user=$(whoami) &
    fi
    MYSQLD_PID=$!

    for i in {1..300}; do
        if ! kill -0 ${MYSQLD_PID} 2>/dev/null; then
            log_error "mysqld died during startup; see ${SERVER_DATA_DIR}/mysql-error.log"
            return 1
        fi
        if "${MYSQL_CLIENT}" --socket="${MYSQL_SOCKET}" -u root -e "SELECT 1" >/dev/null 2>&1; then
            log_info "mysqld ready (PID ${MYSQLD_PID})"
            break
        fi
        [ $i -eq 300 ] && { log_error "mysqld did not become ready"; return 1; }
        sleep 2
    done

    sleep 2
    if ! check_allocator_loaded ${MYSQLD_PID} "${allocator}"; then
        log_error "Allocator verification FAILED: ${allocator} not in effect in mysqld"
        return 1
    fi
    log_info "Allocator verification passed: ${allocator}"
    return 0
}

# ---------------------------------------------------------------------------
# Data directory: initial load and per-rep snapshot restore
# ---------------------------------------------------------------------------
initial_load() {
    log_info "Initializing fresh data directory and loading TPC-C schema..."
    rm -rf "${SERVER_DATA_DIR}"
    mkdir -p "${SERVER_DATA_DIR}"
    "${SERVER_BINARY}" --no-defaults --initialize-insecure --user=$(whoami) \
        --datadir="${SERVER_DATA_DIR}" || return 1

    start_mysqld glibc || return 1
    "${MYSQL_CLIENT}" --socket="${MYSQL_SOCKET}" -u root <<EOF || return 1
CREATE USER IF NOT EXISTS 'tpcuser'@'%' IDENTIFIED BY 'tpcpass';
GRANT ALL PRIVILEGES ON *.* TO 'tpcuser'@'%' WITH GRANT OPTION;
CREATE USER IF NOT EXISTS 'tpcuser'@'localhost' IDENTIFIED BY 'tpcpass';
GRANT ALL PRIVILEGES ON *.* TO 'tpcuser'@'localhost' WITH GRANT OPTION;
FLUSH PRIVILEGES;
EOF
    log_info "Building TPC-C database (this may take a while)..."
    "${HAMMERDB_CLI}" auto "${HAMMERDB_LOAD_TCL}" || { log_error "DB build failed"; return 1; }
    cleanup_run
    sleep 3
    return 0
}

make_snapshot() {
    log_info "Creating data directory snapshot: ${SNAPSHOT_DIR}"
    rm -rf "${SNAPSHOT_DIR}"
    # --reflink=auto is instant on XFS/btrfs, falls back to a full copy on ext4
    cp -a --reflink=auto "${SERVER_DATA_DIR}" "${SNAPSHOT_DIR}" || return 1
    return 0
}

restore_snapshot() {
    log_info "Restoring data directory from snapshot..."
    rm -rf "${SERVER_DATA_DIR}"
    cp -a --reflink=auto "${SNAPSHOT_DIR}" "${SERVER_DATA_DIR}" || return 1
    return 0
}

# ---------------------------------------------------------------------------
# Collectors: proc sampling + performance_schema tracked memory
# ---------------------------------------------------------------------------
run_collectors() {
    local pid=$1 results_dir=$2 prefix=$3 dt=$4 run_flag=$5
    local rollup="${results_dir}/${prefix}_mysql_smaps_rollup_${dt}.log"
    local maps="${results_dir}/${prefix}_mysql_maps_${dt}.log"
    local smaps="${results_dir}/${prefix}_mysql_smaps_${dt}.log"
    local rss="${results_dir}/${prefix}_rss_memory_${dt}.log"
    local psmem="${results_dir}/${prefix}_ps_memory_${dt}.csv"

    echo "# MySQL /proc/${pid}/smaps_rollup data collection" > "${rollup}"
    echo "# MySQL /proc/${pid}/maps data collection" > "${maps}"
    echo "# MySQL /proc/${pid}/smaps data collection (every 60 seconds)" > "${smaps}"
    { echo "# mysqld memory log (every 5 seconds), PID ${pid}"
      echo "# Timestamp, VmRSS_KB, VmSize_KB"; } > "${rss}"
    echo "timestamp,tracked_bytes" > "${psmem}"

    local iteration=0 ts
    while kill -0 ${pid} 2>/dev/null && [ -f "${run_flag}" ]; do
        ts=$(date +"%Y-%m-%d %H:%M:%S")

        echo "=== ${ts} ===" >> "${rollup}"
        cat "/proc/${pid}/smaps_rollup" >> "${rollup}" 2>/dev/null || true
        echo "" >> "${rollup}"

        awk -v ts="${ts}" '/^VmRSS:/{r=$2} /^VmSize:/{s=$2} END{printf "%s, %s, %s\n", ts, r, s}' \
            "/proc/${pid}/status" >> "${rss}" 2>/dev/null || true

        if [ $((iteration % 6)) -eq 0 ]; then
            echo "=== ${ts} ===" >> "${maps}"
            cat "/proc/${pid}/maps" >> "${maps}" 2>/dev/null || true
            echo "" >> "${maps}"

            "${MYSQL_CLIENT}" --socket="${MYSQL_SOCKET}" -u root -N -B -e \
                "SELECT CONCAT('${ts}', ',', COALESCE(SUM(CURRENT_NUMBER_OF_BYTES_USED),0)) \
                 FROM performance_schema.memory_summary_global_by_event_name" \
                 >> "${psmem}" 2>/dev/null || true
        fi

        if [ $((iteration % 12)) -eq 0 ]; then
            echo "=== ${ts} ===" >> "${smaps}"
            cat "/proc/${pid}/smaps" >> "${smaps}" 2>/dev/null || true
            echo "" >> "${smaps}"
        fi

        iteration=$((iteration + 1))
        sleep 5
    done
}

# ---------------------------------------------------------------------------
# Malloc-stress clients: allocation-heavy SQL + connection churn.
# Each iteration is a new mysql client process (connect/disconnect cycle)
# running queries that force large transient allocations.
# ---------------------------------------------------------------------------
stress_client_loop() {
    local warehouses=$1 client_id=$2
    while true; do
        local w=$(( (RANDOM % warehouses) + 1 ))
        "${MYSQL_CLIENT}" --socket="${MYSQL_SOCKET}" -u tpcuser -ptpcpass tpcc \
            --comments -e "
/* allocator-stress-marker client ${client_id} */
SET SESSION sort_buffer_size = 67108864;
SET SESSION tmp_table_size = 268435456;
SET SESSION max_heap_table_size = 268435456;
SET SESSION group_concat_max_len = 8388608;
SELECT ol_number, COUNT(*), SUM(ol_amount),
       LENGTH(GROUP_CONCAT(ol_dist_info ORDER BY ol_amount DESC))
  FROM order_line WHERE ol_w_id = ${w} GROUP BY ol_number
 ORDER BY SUM(ol_amount) DESC;
SELECT c_last, c_credit, COUNT(*) cnt, AVG(c_balance)
  FROM customer WHERE c_w_id = ${w}
 GROUP BY c_last, c_credit ORDER BY cnt DESC, c_last LIMIT 500;
PREPARE s FROM 'SELECT s_i_id, s_quantity, s_dist_01 FROM stock
                 WHERE s_w_id = ? ORDER BY s_quantity DESC, s_dist_01 LIMIT 200';
SET @w = ${w};
EXECUTE s USING @w;
EXECUTE s USING @w;
DEALLOCATE PREPARE s;
SELECT LENGTH(JSON_ARRAYAGG(JSON_OBJECT('c', c_city, 'b', c_balance)))
  FROM customer WHERE c_w_id = ${w} AND c_d_id <= 5;
" > /dev/null 2>&1
        sleep 0.2
    done
}

start_stress_clients() {
    local warehouses
    warehouses=$("${MYSQL_CLIENT}" --socket="${MYSQL_SOCKET}" -u root -N -B \
        -e "SELECT COUNT(*) FROM tpcc.warehouse" 2>/dev/null)
    if [ -z "${warehouses}" ] || [ "${warehouses}" -lt 1 ]; then
        log_warn "Could not determine warehouse count; stress clients disabled"
        return
    fi
    log_info "Starting ${STRESS_CLIENTS} malloc-stress clients (warehouses: ${warehouses})"
    for i in $(seq 1 "${STRESS_CLIENTS}"); do
        stress_client_loop "${warehouses}" "$i" &
        STRESS_PIDS+=($!)
    done
}

# ---------------------------------------------------------------------------
# HammerDB timed run for one load phase; returns via globals
# ---------------------------------------------------------------------------
run_hammerdb_phase() {
    local phase=$1 duration_min=$2 rampup_min=$3 output_file=$4
    local run_tcl="${SCRIPT_DIR}/hammerdb_run_suite.tcl"

    cat > "${run_tcl}" <<EOF
#!/usr/bin/tclsh
source ${SCRIPT_DIR}/mysqloltp.tcl
dbset db mysql
dbset bm TPC-C
diset connection mysql_host localhost
diset connection mysql_socket ${MYSQL_SOCKET}
diset connection mysql_ssl false
diset tpcc mysql_user tpcuser
diset tpcc mysql_pass tpcpass
diset tpcc mysql_dbase tpcc
diset tpcc mysql_driver timed
diset tpcc mysql_rampup ${rampup_min}
diset tpcc mysql_duration ${duration_min}
diset tpcc mysql_allwarehouse true
diset tpcc mysql_timeprofile false
diset tpcc mysql_history_pk true
diset tpcc mysql_no_stored_procs true
diset tpcc mysql_total_iterations 100000000
vuset vu ${VIRTUAL_USERS}
vucreate
if {[catch {vurun} result]} {
    puts "ERROR during vurun: \$result"
    exit 1
}
EOF

    log_info "Phase '${phase}': ${VIRTUAL_USERS} VUs, rampup ${rampup_min}m + ${duration_min}m"
    "${HAMMERDB_CLI}" auto "${run_tcl}" > "${output_file}" 2>&1 &
    HAMMERDB_PID=$!
}

# For --thp=yes: verify huge pages actually materialized once under load
check_thp_effective() {
    local pid=$1
    local ahp
    ahp=$(grep '^AnonHugePages:' "/proc/${pid}/smaps_rollup" 2>/dev/null | awk '{print $2}')
    if [ -z "${ahp}" ] || [ "${ahp}" -eq 0 ]; then
        log_error "THP verification FAILED: AnonHugePages is ${ahp:-unreadable} kB under load"
        log_error "THP run would be meaningless; aborting suite."
        return 1
    fi
    log_info "THP verification passed: AnonHugePages = ${ahp} kB"
    return 0
}

# ---------------------------------------------------------------------------
# One full run: steady -> idle -> regrow for a given allocator + repetition
# ---------------------------------------------------------------------------
run_one() {
    local allocator=$1 rep=$2
    local prefix="${THP_ENABLED}_${allocator}"
    local results_dir="${SUITE_DIR}/results-${RESULTS_SUFFIX}-${THP_ENABLED}-${allocator}-rep${rep}-${BUFFER_POOL_SIZE_GB}G"
    local dt run_flag phases_csv

    log_info "================================================================"
    log_info "RUN: allocator=${allocator} thp=${THP_MODE} rep=${rep}/${REPS}"
    log_info "================================================================"

    if [ "${USE_SNAPSHOT}" = "yes" ] && [ -d "${SNAPSHOT_DIR}" ]; then
        restore_snapshot || { log_error "Snapshot restore failed"; return 1; }
    fi

    mkdir -p "${results_dir}"
    dt=$(date +%Y%m%d_%H%M%S)
    run_flag="${results_dir}/.running"
    phases_csv="${results_dir}/${prefix}_phases_${dt}.csv"
    echo "phase,start,end" > "${phases_csv}"

    start_mysqld "${allocator}" || return 1

    # Record run configuration for self-documentation
    {
        echo "allocator=${allocator}"
        echo "thp=$(cat ${THP_SYSFS})"
        echo "rep=${rep}"
        echo "buffer_pool_gb=${BUFFER_POOL_SIZE_GB}"
        echo "vu=${VIRTUAL_USERS}"
        echo "stress_clients=${STRESS_CLIENTS}"
        echo "phases=steady:${STEADY_MINUTES}m,idle:${IDLE_MINUTES}m,regrow:${REGROW_MINUTES}m"
        echo "rampup=${RAMPUP_MINUTES}m"
        grep 'LD_PRELOAD\|libjemalloc\|libtcmalloc' "/proc/${MYSQLD_PID}/maps" 2>/dev/null | head -2
    } > "${results_dir}/${prefix}_run_config_${dt}.log"

    touch "${run_flag}"
    run_collectors ${MYSQLD_PID} "${results_dir}" "${prefix}" "${dt}" "${run_flag}" &
    COLLECTOR_PID=$!

    local phase_start phase_end

    # --- Phase 1: steady ---------------------------------------------------
    phase_start=$(date +"%Y-%m-%d %H:%M:%S")
    run_hammerdb_phase "steady" "${STEADY_MINUTES}" "${RAMPUP_MINUTES}" \
        "${results_dir}/${prefix}_hammerdb_steady_${dt}.log"
    sleep 30
    start_stress_clients

    # THP must be observable a few minutes into load
    if [ "${THP_MODE}" = "yes" ]; then
        sleep 240
        check_thp_effective ${MYSQLD_PID} || { rm -f "${run_flag}"; cleanup_run; return 1; }
    fi

    wait ${HAMMERDB_PID} 2>/dev/null
    HAMMERDB_PID=""
    stop_stress_clients
    phase_end=$(date +"%Y-%m-%d %H:%M:%S")
    echo "steady,${phase_start},${phase_end}" >> "${phases_csv}"
    log_info "Phase 'steady' complete"

    # --- Phase 2: idle (release-to-OS behavior) ----------------------------
    phase_start=$(date +"%Y-%m-%d %H:%M:%S")
    log_info "Phase 'idle': no load for ${IDLE_MINUTES} minutes (RSS decay)"
    sleep $((IDLE_MINUTES * 60))
    phase_end=$(date +"%Y-%m-%d %H:%M:%S")
    echo "idle,${phase_start},${phase_end}" >> "${phases_csv}"
    log_info "Phase 'idle' complete"

    # --- Phase 3: regrow (fragmentation ratchet) ---------------------------
    phase_start=$(date +"%Y-%m-%d %H:%M:%S")
    run_hammerdb_phase "regrow" "${REGROW_MINUTES}" "${RAMPUP_MINUTES}" \
        "${results_dir}/${prefix}_hammerdb_regrow_${dt}.log"
    sleep 30
    start_stress_clients
    wait ${HAMMERDB_PID} 2>/dev/null
    HAMMERDB_PID=""
    stop_stress_clients
    phase_end=$(date +"%Y-%m-%d %H:%M:%S")
    echo "regrow,${phase_start},${phase_end}" >> "${phases_csv}"
    log_info "Phase 'regrow' complete"

    # Stop collectors, shut down mysqld
    rm -f "${run_flag}"
    sleep 6
    cleanup_run

    # --- Per-run summary rows ----------------------------------------------
    python3 - "${results_dir}" "${prefix}" "${dt}" "${allocator}" "${THP_ENABLED}" "${rep}" "${SUMMARY_CSV}" <<'PYEOF'
import csv, re, sys
from datetime import datetime

results_dir, prefix, dt, allocator, thp, rep, summary_csv = sys.argv[1:8]

def ts(s): return datetime.strptime(s, '%Y-%m-%d %H:%M:%S')

phases = []
with open(f'{results_dir}/{prefix}_phases_{dt}.csv') as f:
    for row in csv.DictReader(f):
        phases.append((row['phase'], ts(row['start']), ts(row['end'])))

rss = []   # (time, rss_kb)
with open(f'{results_dir}/{prefix}_rss_memory_{dt}.log') as f:
    for line in f:
        m = re.match(r'^([\d-]+ [\d:]+),\s*(\d+),', line)
        if m: rss.append((ts(m.group(1)), int(m.group(2))))

tracked = []  # (time, bytes)
try:
    with open(f'{results_dir}/{prefix}_ps_memory_{dt}.csv') as f:
        next(f)
        for line in f:
            parts = line.strip().split(',')
            if len(parts) == 2 and parts[1].isdigit():
                tracked.append((ts(parts[0]), int(parts[1])))
except FileNotFoundError:
    pass

def nopm(path):
    try:
        txt = open(path, errors='replace').read()
        m = re.findall(r'achieved (\d+) NOPM from (\d+)', txt)
        return (int(m[-1][0]), int(m[-1][1])) if m else ('', '')
    except FileNotFoundError:
        return ('', '')

hammer = {'steady': nopm(f'{results_dir}/{prefix}_hammerdb_steady_{dt}.log'),
          'regrow': nopm(f'{results_dir}/{prefix}_hammerdb_regrow_{dt}.log'),
          'idle': ('', '')}

with open(summary_csv, 'a', newline='') as f:
    w = csv.writer(f)
    for phase, start, end in phases:
        prss = [v for t, v in rss if start <= t <= end]
        ptrk = [v for t, v in tracked if start <= t <= end]
        n, t_ = hammer.get(phase, ('', ''))
        w.writerow([allocator, thp, rep, phase,
                    start.strftime('%Y-%m-%d %H:%M:%S'),
                    end.strftime('%Y-%m-%d %H:%M:%S'), n, t_,
                    round(sum(prss)/len(prss)/1024, 1) if prss else '',
                    round(max(prss)/1024, 1) if prss else '',
                    round(prss[-1]/1024, 1) if prss else '',
                    round(sum(ptrk)/len(ptrk)/1024/1024, 1) if ptrk else ''])
print(f"Summary rows appended to {summary_csv}")
PYEOF

    log_info "Run complete: ${results_dir}"
    return 0
}

# ---------------------------------------------------------------------------
# Suite main
# ---------------------------------------------------------------------------
log_info "Allocator performance suite: allocators=[${ALLOCATORS}] thp=${THP_MODE} reps=${REPS}"
log_info "Phases per run: steady ${STEADY_MINUTES}m -> idle ${IDLE_MINUTES}m -> regrow ${REGROW_MINUTES}m (+ 2x ${RAMPUP_MINUTES}m rampup)"
log_info "Suite directory: ${SUITE_DIR}"

log_info "Killing any existing mysqld processes..."
sudo killall mysqld 2>/dev/null || true
sleep 2

setup_cpu
write_my_cnf
if [ "${THP_MODE}" = "yes" ]; then set_thp always; else set_thp never; fi

if [ "${SKIP_INIT}" = "noskip" ]; then
    initial_load || { log_error "Initial load failed"; exit 1; }
    [ "${USE_SNAPSHOT}" = "yes" ] && { make_snapshot || exit 1; }
else
    [ -d "${SERVER_DATA_DIR}" ] || { log_error "No data directory: ${SERVER_DATA_DIR} (use --skip-init=no)"; exit 1; }
    if [ "${USE_SNAPSHOT}" = "yes" ] && [ ! -d "${SNAPSHOT_DIR}" ]; then
        make_snapshot || exit 1
    fi
fi

FAILED_RUNS=()
for rep in $(seq 1 "${REPS}"); do
    for allocator in "${ALLOCATOR_LIST[@]}"; do
        if ! run_one "${allocator}" "${rep}"; then
            log_error "Run failed: ${allocator} rep ${rep}"
            FAILED_RUNS+=("${allocator}-rep${rep}")
            cleanup_run
        fi
        sleep 5
    done
done

log_info "================================================================"
log_info "Suite finished. Summary: ${SUMMARY_CSV}"
if [ ${#FAILED_RUNS[@]} -gt 0 ]; then
    log_warn "Failed runs: ${FAILED_RUNS[*]}"
fi
log_info "Per-run logs use the standard naming; generate reports with e.g.:"
log_info "  ./generate_maps_report.py  suite-${RESULTS_SUFFIX} maps_report-${RESULTS_SUFFIX}.html"
log_info "  ./generate_smaps_report.py suite-${RESULTS_SUFFIX} smaps_report-${RESULTS_SUFFIX}.html"
column -s, -t "${SUMMARY_CSV}" | head -40 || true
