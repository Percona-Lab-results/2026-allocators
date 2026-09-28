-- Per-query allocation stress for the allocator perf suite.
--
-- Runs against the HammerDB TPC-C schema (read-only) and magnifies
-- per-statement allocations:
--   * sort/join/temp-table buffers with RANDOMIZED sizes per event, so the
--     allocator sees varying large allocations instead of one easy size class
--   * internal temp tables + filesort (GROUP BY ... ORDER BY aggregate)
--   * GROUP_CONCAT with a large group_concat_max_len
--   * real binary-protocol prepared-statement churn (prepare/execute/close)
--   * large IN-list statements (big parser/Item trees in the statement
--     MEM_ROOT)
--
-- Usage:
--   sysbench lua/alloc_query.lua --mysql-socket=... --mysql-user=tpcuser \
--       --mysql-password=tpcpass --mysql-db=tpcc --threads=4 --time=0 run

sysbench.cmdline.options = {
    warehouses = {"Warehouse count (0 = detect from tpcc.warehouse)", 0},
    reconnect_every = {"Reconnect after every N events (0 = never)", 0},
    inlist_size = {"Number of literals in the big IN-list query", 300},
}

local function detect_warehouses(c)
    local ok, count = pcall(function()
        return c:query_row("SELECT COUNT(*) FROM warehouse")
    end)
    count = ok and tonumber(count) or 0
    return count > 0 and count or 100
end

function thread_init()
    drv = sysbench.sql.driver()
    con = drv:connect()
    warehouses = sysbench.opt.warehouses > 0 and sysbench.opt.warehouses
                 or detect_warehouses(con)
    events_done = 0
end

function thread_done()
    con:disconnect()
end

local function set_random_buffers()
    -- Randomized sizes defeat size-class caching and expose fragmentation:
    -- sort 256K..64M, join 256K..32M, tmp tables 16M..256M.
    con:query(string.format([[
SET SESSION sort_buffer_size = %d,
    join_buffer_size = %d,
    tmp_table_size = %d,
    max_heap_table_size = %d,
    group_concat_max_len = 8388608]],
        sysbench.rand.uniform(256, 65536) * 1024,
        sysbench.rand.uniform(256, 32768) * 1024,
        sysbench.rand.uniform(16, 256) * 1048576,
        sysbench.rand.uniform(16, 256) * 1048576))
end

local function q_sort_temp(w)
    -- ~100k order_line rows grouped and filesorted by a computed aggregate
    con:query(string.format([[
SELECT ol_i_id, COUNT(*) c, SUM(ol_amount) s FROM order_line
 WHERE ol_w_id = %d GROUP BY ol_i_id ORDER BY s DESC, c LIMIT 100]], w))
end

local function q_group_concat(w)
    con:query(string.format([[
SELECT ol_number, LENGTH(GROUP_CONCAT(ol_dist_info ORDER BY ol_amount DESC))
  FROM order_line WHERE ol_w_id = %d GROUP BY ol_number]], w))
end

local function q_customer_group(w)
    con:query(string.format([[
SELECT c_last, c_credit, COUNT(*) cnt, AVG(c_balance)
  FROM customer WHERE c_w_id = %d
 GROUP BY c_last, c_credit ORDER BY cnt DESC, c_last LIMIT 500]], w))
end

local function q_ps_churn(w)
    -- Binary-protocol server-side PS: allocate stmt structures, execute
    -- with different params, free - per event.
    local stmt = con:prepare([[
SELECT s_i_id, s_quantity, s_dist_01 FROM stock
 WHERE s_w_id = ? AND s_quantity > ? ORDER BY s_quantity DESC, s_dist_01 LIMIT 200]])
    local p1 = stmt:bind_create(sysbench.sql.type.INT)
    local p2 = stmt:bind_create(sysbench.sql.type.INT)
    stmt:bind_param(p1, p2)
    for i = 1, 3 do
        p1:set(w)
        p2:set(sysbench.rand.uniform(0, 50))
        stmt:execute()
    end
    stmt:close()
end

local function q_big_inlist()
    -- Statement text with hundreds of literals: the parse/Item tree (and the
    -- statement MEM_ROOT holding it) scales with the list size.
    local ids = {}
    for i = 1, sysbench.opt.inlist_size do
        ids[i] = sysbench.rand.uniform(1, 100000)
    end
    con:query(string.format(
        "SELECT COUNT(*), SUM(i_price) FROM item WHERE i_id IN (%s)",
        table.concat(ids, ",")))
end

function event()
    if sysbench.opt.reconnect_every > 0 and
       events_done % sysbench.opt.reconnect_every == 0 and events_done > 0 then
        con:disconnect()
        con = drv:connect()
    end
    events_done = events_done + 1

    local w = sysbench.rand.uniform(1, warehouses)
    set_random_buffers()

    local branch = sysbench.rand.uniform(1, 5)
    if branch == 1 then
        q_sort_temp(w)
    elseif branch == 2 then
        q_group_concat(w)
    elseif branch == 3 then
        q_customer_group(w)
    elseif branch == 4 then
        q_ps_churn(w)
    else
        q_big_inlist()
    end
end
