-- Server-internal allocation stress for the allocator perf suite.
--
-- Uses its own schema (allocstress) so it never conflicts with the TPC-C
-- workload's rows or locks. Magnifies allocations the server makes for
-- itself:
--   * table/dictionary cache churn: touch random tables out of a large set
--     (optionally FLUSH TABLES periodically to force full reopen storms)
--   * lock-system heaps: transactions holding hundreds of row locks
--   * purge pressure: UPDATE bursts building undo the purge threads chew
--     through in their own heaps (visible during the suite's idle phase)
--   * DDL churn: CREATE/DROP TABLE allocating dictionary objects
--
-- Table setup runs in thread_init; each thread creates/seeds its own subset
-- (table index modulo thread count), so setup parallelizes and re-running
-- against an existing schema is a no-op.
--
-- Usage:
--   sysbench lua/alloc_internal.lua --mysql-socket=... --mysql-user=tpcuser \
--       --mysql-password=tpcpass --mysql-db=tpcc --threads=2 --time=0 run

sysbench.cmdline.options = {
    tables = {"Number of stress tables in schema allocstress", 128},
    rows = {"Rows per stress table", 500},
    flush_every = {"FLUSH TABLES every N events on thread 0 (0 = off)", 0},
    engine = {"Storage engine for stress tables (innodb|rocksdb)", "innodb"},
}

local DB = "allocstress"

local function tname(i)
    return string.format("%s.t_%04d", DB, i)
end

local function seed_table(i)
    local t = tname(i)
    con:query(string.format([[
CREATE TABLE IF NOT EXISTS %s (
  id INT NOT NULL PRIMARY KEY,
  grp INT NOT NULL,
  filler VARCHAR(200) NOT NULL,
  KEY (grp)
) ENGINE=%s]], t, sysbench.opt.engine))
    local ok, count = pcall(function()
        return con:query_row("SELECT COUNT(*) FROM " .. t)
    end)
    if ok and tonumber(count) >= sysbench.opt.rows then
        return
    end
    local vals = {}
    for r = 1, sysbench.opt.rows do
        vals[r] = string.format("(%d, %d, REPEAT('f', 180))", r, r % 50)
    end
    con:query(string.format("REPLACE INTO %s (id, grp, filler) VALUES %s",
                            t, table.concat(vals, ",")))
end

function thread_init()
    drv = sysbench.sql.driver()
    con = drv:connect()
    events_done = 0

    if sysbench.tid == 0 then
        con:query("CREATE DATABASE IF NOT EXISTS " .. DB)
    end
    -- Everyone needs the database before creating tables; cheap to repeat.
    pcall(function() con:query("CREATE DATABASE IF NOT EXISTS " .. DB) end)

    for i = 0, sysbench.opt.tables - 1 do
        if i % sysbench.opt.threads == sysbench.tid then
            seed_table(i)
        end
    end
end

function thread_done()
    con:disconnect()
end

local function rand_table()
    return tname(sysbench.rand.uniform(0, sysbench.opt.tables - 1))
end

local function b_table_cache()
    -- Touch several random tables: TABLE object + handler allocations when
    -- the table is not in the open cache (always, after a FLUSH TABLES).
    for i = 1, 8 do
        con:query(string.format(
            "SELECT SUM(LENGTH(filler)) FROM %s WHERE grp = %d",
            rand_table(), sysbench.rand.uniform(0, 49)))
    end
end

local function b_lock_heap()
    -- Hundreds of row locks held in the transaction's lock heap, then freed
    con:query("BEGIN")
    con:query(string.format("SELECT COUNT(*) FROM %s FOR UPDATE", rand_table()))
    con:query(string.format("SELECT COUNT(*) FROM %s FOR UPDATE", rand_table()))
    con:query("COMMIT")
end

local function b_purge_pressure()
    -- Update bursts generate undo; purge threads allocate while draining it
    local a = sysbench.rand.uniform(1, math.max(1, sysbench.opt.rows - 100))
    con:query(string.format(
        "UPDATE %s SET filler = REPEAT(CHAR(97 + %d), 180) WHERE id BETWEEN %d AND %d",
        rand_table(), sysbench.rand.uniform(0, 25), a, a + 100))
end

local function b_ddl_churn()
    local t = string.format("%s.ddl_%d", DB, sysbench.tid)
    con:query("DROP TABLE IF EXISTS " .. t)
    con:query(string.format([[
CREATE TABLE %s (id INT PRIMARY KEY, v VARCHAR(64), KEY (v)) ENGINE=%s]],
        t, sysbench.opt.engine))
    con:query(string.format(
        "INSERT INTO %s VALUES (1, 'x'), (2, 'y'), (3, 'z')", t))
    con:query("DROP TABLE " .. t)
end

function event()
    events_done = events_done + 1

    if sysbench.opt.flush_every > 0 and sysbench.tid == 0 and
       events_done % sysbench.opt.flush_every == 0 then
        con:query("FLUSH TABLES")
    end

    local branch = sysbench.rand.uniform(1, 10)
    if branch <= 5 then
        b_table_cache()
    elseif branch <= 7 then
        b_lock_heap()
    elseif branch <= 9 then
        b_purge_pressure()
    else
        b_ddl_churn()
    end
end
