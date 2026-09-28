-- Per-connection allocation stress for the allocator perf suite.
--
-- Every event is a full connect -> work -> disconnect cycle, so the server
-- allocates and frees a THD, network buffers and (for new threads) a thread
-- stack at the event rate. The optional "balloon" query returns a multi-MB
-- result so the connection's net buffer grows from net_buffer_length toward
-- max_allowed_packet before the whole connection is torn down.
--
-- Usage:
--   sysbench lua/alloc_conn.lua --mysql-socket=... --mysql-user=tpcuser \
--       --mysql-password=tpcpass --mysql-db=tpcc --threads=4 --time=0 run

sysbench.cmdline.options = {
    queries_per_conn = {"Trivial queries per connection", 2},
    balloon_kb = {"Big result fetched once per connection (KB, 0 = off)", 2048},
}

function thread_init()
    drv = sysbench.sql.driver()
end

function event()
    local con = drv:connect()
    for i = 1, sysbench.opt.queries_per_conn do
        con:query("SELECT 1")
    end
    if sysbench.opt.balloon_kb > 0 then
        -- Server builds the string and streams it through the growing
        -- net buffer; freed with the THD on disconnect.
        con:query(string.format("SELECT REPEAT('x', %d)",
                                sysbench.opt.balloon_kb * 1024))
    end
    con:disconnect()
end
