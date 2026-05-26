### **Phase 1: Test Objective & Configuration**

**Primary Objective:** Examine memory allocator behavior during InnoDB page creation and deletion operations. The test will evaluate how each allocator handles high-volume memory allocation/deallocation patterns, fragmentation, and long-running memory stability.

**Target Configuration:**
* **Memory Usage Target:** 150 GB working set  
* **Database Size:** 300 GB (3,000 warehouses in HammerDB TPC-C)  
* **Storage Engine:** InnoDB only  
* **Connection Method:** MySQL Unix socket (eliminates TCP/IP overhead)  
* **Virtual Users:** 80 VU in HammerDB

**MySQL Configuration for Minimal Logging Overhead:**

```ini
# Disable binary logging
skip-log-bin

# InnoDB redo log configuration
innodb_redo_log_capacity = 32G

# Minimize flush overhead (not crash-safe, but optimal for testing)
innodb_flush_log_at_trx_commit = 0

# Memory configuration to target 150 GB buffer pool
innodb_buffer_pool_size = 150G
innodb_buffer_pool_instances = 16
```

### **Phase 2: Environment Setup & Team Coordination**

* **Coordinate with Packaging/Build Team:** Before testing begins, align with the packaging team regarding the request to change the bundled allocator. Ensure they are prepared to swap out the currently bundled `jemalloc 3.6` for `jemalloc 5.3` (or another chosen allocator) based on the final results.  
* **Standardize Hardware:** Provision identical infrastructure (beast nodes) for testing on **x86 instances** with sufficient memory capacity 187 GB RAM to support the target workload.  
* **CPU Performance Configuration:** Set CPU governor to performance mode and disable CPU idle states for consistent performance:
  ```Shell
  # Set CPU governor to performance mode
  echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
  
  # Disable CPU idle states
  sudo cpupower idle-set -D 0
  
  # Verify settings
  cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
  cpupower idle-info
  ```
* **Prepare Benchmarking Tools:** Set up `HammerDB` configured for extended-duration TPC-C workloads.  
* **Install the Target Allocators:** Ensure all 4 allocators are available on the test nodes. *(Note: You may need to manually compile or fetch specific package versions for older/newer jemallocs).*  
  * `glibc` (OS default ptmalloc)  
  * `tcmalloc`  
  * `jemalloc 3.6` (Current PS bundled version)  
  * `jemalloc 5.3` (Proposed upgrade)

#### **Step-by-Step Allocator Configuration Guide**

You will cycle through these configurations for each of your test runs. After applying any of these configurations, always verify it loaded correctly by checking the process map: `cat /proc/$(pgrep mysqld)/maps | grep -i 'jemalloc\|tcmalloc'`.

**1\. Configuring for `glibc` (The Baseline)** Since `glibc` (ptmalloc) is the default Linux allocator, ensure no custom systemd overrides exist:

Bash

```
sudo rm -f /etc/systemd/system/mysql.service.d/override.conf
sudo systemctl daemon-reload
sudo systemctl restart mysql
```

**2\. Configuring for `jemalloc` (Versions 3.6 or 5.3)**

* Find the exact path for the specific version installed (e.g., `dpkg -L libjemalloc2 | grep libjemalloc.so`).  
* Create the override: `sudo systemctl edit mysql`  
* Add the configuration:  
* Ini, TOML

```
[Service]
Environment="LD_PRELOAD=/path/to/specific/libjemalloc.so"
```

*   
* Apply changes: `sudo systemctl daemon-reload && sudo systemctl restart mysql`
* Enable large pages by adding to my.cnf `[mysqld]` section:

```
large-pages=ON
```

**3\. Configuring for `tcmalloc`**

* Find the exact path (e.g., `dpkg -L libtcmalloc-minimal4 | grep libtcmalloc_minimal.so`).  
* Create the override: `sudo systemctl edit mysql`  
* Add the configuration:  
* Ini, TOML

```
[Service]
Environment="LD_PRELOAD=/path/to/specific/libtcmalloc_minimal.so"
```

*   
* Apply changes: `sudo systemctl daemon-reload && sudo systemctl restart mysql`

### **Phase 3: Test Matrix & Variables**

Tests will be executed across an **8-combination matrix (4 Allocators × 2 THP States)**:

* **Allocators:**  
  * `glibc` (baseline)  
  * `jemalloc 3.6` (current bundled)  
  * `jemalloc 5.3` (upgrade candidate)  
  * `tcmalloc`  
* **Transparent Huge Pages (THP) States:**  
  * **Disabled:** `echo never > /sys/kernel/mm/transparent_hugepage/enabled`  
  * **Enabled:** `echo always > /sys/kernel/mm/transparent_hugepage/enabled`  
* **Storage Engine:**  
  * **InnoDB:** Primary focus for page creation/deletion analysis.
* **Test Duration:** 10-20 hours per configuration to capture long-running memory behavior and fragmentation patterns.

**Transparent Huge Pages Monitoring:**

When THP is enabled, verify MySQL is actually using huge pages:
```Shell
# Check THP allocation for MySQL process
grep AnonHugePages /proc/$(pgrep mysqld)/status

# Monitor THP stats system-wide
grep thp /proc/vmstat
```

Expected behavior when THP is working:
* `AnonHugePages` should show significant values (multiple GB)
* `/proc/vmstat` counters `thp_fault_alloc`, `thp_collapse_alloc` should increase during the test

### **Phase 4: Test Execution Strategy**

For **each allocator**, run the following sequence:

**Step 1: THP Disabled Testing**
1. Disable THP: `echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled`
2. Configure the allocator via systemd override
3. Restart MySQL and verify allocator is loaded
4. Build the 300 GB database (3,000 warehouses) using HammerDB
5. Start memory monitoring (see Phase 5)
6. Run HammerDB TPC-C with 80 VU for 10-20 hours
7. Capture final memory metrics and MySQL error logs
8. Archive all monitoring data

**Step 2: THP Enabled Testing**
1. Enable THP: `echo always | sudo tee /sys/kernel/mm/transparent_hugepage/enabled`
2. Same allocator configuration remains active
3. Enable large pages by adding to my.cnf `[mysqld]` section:

```
large-pages=ON
```

4. Restart MySQL and verify allocator is loaded
5. Rebuild the database or restore from backup
6. Start memory monitoring with THP metrics
7. Run HammerDB TPC-C with 80 VU for 10-20 hours
8. Verify THP is being used (check `AnonHugePages` > 0)
9. Capture final memory metrics and MySQL error logs
10. Archive all monitoring data

**Step 3: Repeat for Next Allocator**

**Test Execution Order:**
1. `glibc` (THP disabled → THP enabled)
2. `jemalloc 3.6` (THP disabled → THP enabled)
3. `jemalloc 5.3` (THP disabled → THP enabled) — *Monitor MyRocks stability closely*
4. `tcmalloc` (THP disabled → THP enabled)

### **Phase 5: Memory Monitoring & Data Capture**

**Critical Requirement:** Continuous memory metric collection throughout the entire 10-20 hour test duration. Capture data points every 1 second.

#### **Data Sources & Metrics**

**1. /proc/\<pid\>/status** — Primary RSS and memory breakdown
* `VmRSS` — Resident Set Size (total physical memory)
* `VmSize` — Virtual memory size
* `VmSwap` — Swapped memory
* `RssAnon` — Anonymous resident pages
* `RssFile` — File-backed resident pages
* `RssShmem` — Shared memory resident pages

**2. /proc/\<pid\>/smaps_rollup** — Proportional memory accounting
* `Pss` — Proportional Set Size (shared memory divided by sharers)
* `Private_Dirty` — Private dirty pages in RAM

**3. /proc/\<pid\>/smaps** — Per-mapping detailed analysis (collect every 30 sec istead of 1 sec because of large output)

* Aggregate by mapping type:
  * Anonymous mappings (no file backing)
  * `[heap]` mappings
  * File-backed mappings
* Calculate:
  * `anon_vsz_gb` — Total anonymous virtual memory
  * `anon_rss_gb` — Anonymous resident memory
  * `anon_unfaulted_gb` — Anonymous VA not yet faulted in (VSZ - RSS)
  * `heap_size_gb` — Total heap virtual size
  * `heap_rss_gb` — Heap resident size
  * `anon_mapping_count` — Number of anonymous mappings
  * `total_mapping_count` — Total number of mappings

**4. /proc/\<pid\>/stat** — Page fault counters
* `minflt` — Minor page faults (page in memory but not mapped)
* `majflt` — Major page faults (page must be read from disk)

**5. /proc/\<pid\>/maps** — Mapping enumeration (collect every 10 sec istead of 1 sec because of large output)
* Count total mappings over time


**6. Transparent Huge Pages (when enabled)**
* `AnonHugePages` from `/proc/<pid>/status`
* System-wide THP stats from `/proc/vmstat`:
  * `thp_fault_alloc`
  * `thp_fault_fallback`
  * `thp_collapse_alloc`
  * `thp_split_page`

#### **Derived Metrics (19 total columns)**

Timestamp, Allocator, THP_State, VmSize_GB, VmRSS_GB, VmSwap_GB, RssAnon_GB, RssFile_GB, RssShmem_GB, PSS_GB, Private_Dirty_GB, anon_vsz_gb, anon_rss_gb, anon_unfaulted_gb, heap_size_gb, heap_rss_gb, anon_mapping_count, total_mapping_count, minflt, majflt, anon_frag_pct, AnonHugePages_GB

**Calculated Fields:**
```
anon_frag_pct = 100 * (anon_vsz_gb - anon_rss_gb) / anon_vsz_gb
```

#### **Fragmentation & Leak Detection Signals**

Monitor these patterns during the 10-20 hour runs:

1. **Arena Fragmentation (glibc):**  
   * `anon_mapping_count` rising while `RssAnon` is flat → glibc spawning more per-thread arenas under contention

2. **Heap Bloat:**  
   * `heap_size_gb` grows but `heap_rss_gb` is flat → allocator holding virtual address space with no live allocations (glibc rarely returns memory via `sbrk`)

3. **Anonymous Memory Fragmentation:**  
   * `anon_frag_pct` rising after warm-up period → virtual address space committed for anonymous mappings exceeds physical residency = fragmentation or memory leak

4. **Working-Set Thrashing:**  
   * `minflt` rising fast while RSS is stable → excessive cold-page touches, working set thrashing

5. **Major Fault Storm:**  
   * `majflt` increasing significantly → swapping or file I/O pressure

6. **THP Effectiveness (when enabled):**  
   * Low `AnonHugePages` despite high `RssAnon` → THP not being utilized effectively
   * High `thp_fault_fallback` or `thp_split_page` → THP allocation failures or splitting

#### **Monitoring Script Requirements**

Create a monitoring script that:
* Runs continuously for the entire test duration (10-20 hours)
* Samples all metrics every 1 second
* Outputs to timestamped CSV files: `memory_metrics_<allocator>_<thp_state>_<timestamp>.csv`
* Logs MySQL process ID at start and validates PID hasn't changed
* Captures THP metrics when enabled

#### **Additional System Monitoring**

* **MySQL Error Log:** Monitor for crashes, OOM events, or allocator-related segfaults
* **dmesg/syslog:** Check for OOM killer invocations
* **HammerDB Metrics:** Capture transactions per second (TPS) and response times throughout the run

### **Phase 6: Deliverables & Reporting**

Upon completion of all tests, deliver:

1. **Memory Behavior Analysis Report:**  
   * Time-series graphs of all 19 memory metrics for each allocator × THP configuration
   * Fragmentation trend analysis over the 10-20 hour window
   * Identification of memory leaks or unbounded growth patterns
   * Comparison of steady-state memory efficiency across allocators

2. **Transparent Huge Pages Impact Analysis:**  
   * Memory efficiency comparison: THP disabled vs enabled for each allocator
   * THP utilization effectiveness (AnonHugePages achieved)
   * Performance impact (TPS) of THP state

3. **Allocator Stability Assessment:**  
   * Any crashes, hangs, or anomalous behavior observed
   * Long-running stability verdict for each allocator

4. **Bundling Recommendation:**  
   * Data-backed decision on which allocator to bundle with Percona Server 8.4
   * Clear justification based on:
     * Memory efficiency (lowest fragmentation)
     * Long-running stability
     * Performance (TPS)
     * THP compatibility
   * Recommended THP configuration per allocator

5. **Public Documentation:**  
   * Blog post or technical documentation covering:
     * Memory allocator comparison results
     * Best practices for allocator selection by workload type
     * THP configuration recommendations for InnoDB
     * Tuning guidance for production deployments

### **Phase 7: Raw Allocator Micro-Benchmark (Optional Deep Dive)**

To supplement the full-stack database testing, consider using existing allocator benchmark suites to isolate and measure raw allocator performance characteristics.

**Potential Benchmark Candidates:**

1. **mimalloc-bench** (https://github.com/daanx/mimalloc-bench)
   * Comprehensive suite with 30+ real-world allocator benchmarks
   * Includes database-like workloads (cfrac, larson, mstress, cache-scratch)
   * Supports multiple allocators via LD_PRELOAD
   * Measures throughput, memory usage, and fragmentation

2. **malloc-benchmarks** (https://github.com/f18m/malloc-benchmarks)
   * Focused on multi-threaded allocation patterns
   * Includes realistic application scenarios
   * Generates detailed performance reports
   * Easy integration with custom allocators

3. **Real-world simulation approach** (https://6it.dev/blog-484/onprogramming/optimizations/testing-memory-allocators-ptmalloc2-vs-tcmalloc-vs-hoard-vs-jemalloc-while-trying-to-simulate-real-world-loads-776)
   * Demonstrating database workload simulation methodology
   * Cross-thread deallocation patterns
   * Variable allocation sizes mimicking query execution
   * Long-running stability testing

