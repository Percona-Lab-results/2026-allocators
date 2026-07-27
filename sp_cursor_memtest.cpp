// sp_cursor_memtest.cpp
//
// Standalone reproducer for server-side memory growth caused by repeatedly
// opening a materialized cursor inside a stored procedure (the HammerDB
// TPC-C PAYMENT `c_byname` cursor pattern: select customers by last name,
// fetch to the midpoint, close).
//
// The server retains memory for every cursor OPEN on a connection-lifetime
// MEM_ROOT (observed via jemalloc heap profiling:
// sp_instr_copen::execute -> mysql_open_cursor ->
// Materialized_cursor::send_result_set_metadata), released only when the
// connection closes. This test maximizes the effect: N threads with
// persistent connections call a procedure that opens/closes the cursor in a
// tight inner loop, while a monitor thread samples mysqld RSS from
// /proc/<pid>/status and the server's own accounting
// (sys.memory_global_total + top memory events) every report interval.
//
// Expected result: near-linear RSS growth while the test runs; memory is
// returned when the worker connections disconnect at the end (compare the
// final "after disconnect" sample with the last in-flight sample).
//
// Build (against the Percona tarball client library):
//   S=$HOME/servers/Percona-Server-8.4.8-8-Linux.x86_64.glibc2.35
//   g++ -O2 -std=c++17 -o sp_cursor_memtest sp_cursor_memtest.cpp \
//       -I$S/include -L$S/lib -lperconaserverclient -lpthread \
//       -Wl,-rpath,$S/lib
//
// Run:
//   ./sp_cursor_memtest --socket=/tmp/mysql-alloc-test.sock --user=root \
//       --threads=16 --duration=300 --report=10
//
// Options (defaults):
//   --socket=PATH       MySQL unix socket (/tmp/mysql-alloc-test.sock)
//   --host=HOST         TCP host (unset; socket is used unless host given)
//   --port=N            TCP port (3306)
//   --user=NAME         user (root)
//   --password=PW       password (empty)
//   --threads=N         worker connections (16)
//   --duration=N        test duration in seconds (300)
//   --report=N          monitor report interval in seconds (10)
//   --inner-iters=N     cursor opens per CALL, done in an SQL loop (100)
//   --rows-per-name=N   customer rows per last name (100)
//   --skip-setup        reuse existing sp_cursor_test schema

#include <mysql.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace {

struct Config {
  std::string socket = "/tmp/mysql-alloc-test.sock";
  std::string host;
  unsigned port = 3306;
  std::string user = "root";
  std::string password;
  int threads = 16;
  int duration_sec = 300;
  int report_sec = 10;
  int inner_iters = 100;
  int rows_per_name = 100;
  bool skip_setup = false;
};

constexpr const char *kDb = "sp_cursor_test";
constexpr int kLastNames = 20;

std::atomic<long long> g_cursor_opens{0};
std::atomic<long long> g_calls{0};
std::atomic<long long> g_errors{0};
std::atomic<bool> g_stop{false};

void die(const std::string &msg, MYSQL *conn = nullptr) {
  std::fprintf(stderr, "FATAL: %s%s%s\n", msg.c_str(),
               conn ? ": " : "", conn ? mysql_error(conn) : "");
  std::exit(1);
}

MYSQL *connect(const Config &cfg, const char *db) {
  MYSQL *conn = mysql_init(nullptr);
  if (!conn) die("mysql_init failed");
  // CLIENT_MULTI_RESULTS is required for CALL
  if (!mysql_real_connect(conn, cfg.host.empty() ? nullptr : cfg.host.c_str(),
                          cfg.user.c_str(), cfg.password.c_str(), db,
                          cfg.host.empty() ? 0 : cfg.port,
                          cfg.host.empty() ? cfg.socket.c_str() : nullptr,
                          CLIENT_MULTI_RESULTS))
    die("cannot connect to MySQL", conn);
  return conn;
}

// Run a statement and drain every result set it produces.
bool exec(MYSQL *conn, const std::string &sql) {
  if (mysql_query(conn, sql.c_str())) return false;
  do {
    MYSQL_RES *res = mysql_store_result(conn);
    if (res) mysql_free_result(res);
  } while (mysql_next_result(conn) == 0);
  return mysql_errno(conn) == 0;
}

// Run a single-value query, return the value (or fallback on error).
std::string query_value(MYSQL *conn, const std::string &sql,
                        const std::string &fallback = "n/a") {
  if (mysql_query(conn, sql.c_str())) return fallback;
  MYSQL_RES *res = mysql_store_result(conn);
  if (!res) return fallback;
  MYSQL_ROW row = mysql_fetch_row(res);
  std::string out = (row && row[0]) ? row[0] : fallback;
  mysql_free_result(res);
  return out;
}

std::string last_name(int i) { return "NAME" + std::to_string(i % kLastNames); }

void setup_schema(const Config &cfg) {
  MYSQL *conn = connect(cfg, nullptr);

  std::printf("Setting up schema %s (%d last names x %d rows)...\n", kDb,
              kLastNames, cfg.rows_per_name);
  if (!exec(conn, std::string("DROP DATABASE IF EXISTS ") + kDb) ||
      !exec(conn, std::string("CREATE DATABASE ") + kDb) ||
      !exec(conn, std::string("USE ") + kDb))
    die("schema creation failed", conn);

  // Column set mirrors the width of the TPC-C PAYMENT c_byname select list
  if (!exec(conn, R"(
      CREATE TABLE customer (
        c_id        INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        c_first     VARCHAR(16),
        c_middle    CHAR(2),
        c_last      VARCHAR(16),
        c_street_1  VARCHAR(20),
        c_street_2  VARCHAR(20),
        c_city      VARCHAR(20),
        c_state     CHAR(2),
        c_zip       CHAR(9),
        c_phone     CHAR(16),
        c_credit    CHAR(2),
        c_credit_lim DECIMAL(12,2),
        c_discount  DECIMAL(4,4),
        c_balance   DECIMAL(12,2),
        c_since     DATETIME,
        KEY idx_byname (c_last, c_first)
      ) ENGINE=InnoDB)"))
    die("CREATE TABLE failed", conn);

  for (int n = 0; n < kLastNames; ++n) {
    std::ostringstream ins;
    ins << "INSERT INTO customer (c_first, c_middle, c_last, c_street_1,"
           " c_street_2, c_city, c_state, c_zip, c_phone, c_credit,"
           " c_credit_lim, c_discount, c_balance, c_since) VALUES ";
    for (int r = 0; r < cfg.rows_per_name; ++r) {
      if (r) ins << ",";
      ins << "('FIRST" << r << "','OE','" << last_name(n)
          << "','street one 111','street two 222','some city','CA',"
             "'123456789','0123456789012345','GC',50000.00,0.1234,"
             "-10.00,NOW())";
    }
    if (!exec(conn, ins.str())) die("INSERT failed", conn);
  }

  // PAYMENT c_byname pattern: count matches, open cursor, fetch to the
  // midpoint, close. p_iters repeats it inside one CALL to maximize the
  // rate of cursor opens per network round trip.
  if (!exec(conn, R"(
      CREATE PROCEDURE payment_cursor (IN p_c_last VARCHAR(16), IN p_iters INT)
      BEGIN
        DECLARE done INT DEFAULT 0;
        DECLARE namecnt INT;
        DECLARE i INT DEFAULT 0;
        DECLARE j INT;
        DECLARE v_first    VARCHAR(16);
        DECLARE v_middle   CHAR(2);
        DECLARE v_id       INT;
        DECLARE v_street_1 VARCHAR(20);
        DECLARE v_street_2 VARCHAR(20);
        DECLARE v_city     VARCHAR(20);
        DECLARE v_state    CHAR(2);
        DECLARE v_zip      CHAR(9);
        DECLARE v_phone    CHAR(16);
        DECLARE v_credit   CHAR(2);
        DECLARE v_credit_lim DECIMAL(12,2);
        DECLARE v_discount DECIMAL(4,4);
        DECLARE v_balance  DECIMAL(12,2);
        DECLARE v_since    DATETIME;
        DECLARE c_byname CURSOR FOR
          SELECT c_first, c_middle, c_id, c_street_1, c_street_2, c_city,
                 c_state, c_zip, c_phone, c_credit, c_credit_lim,
                 c_discount, c_balance, c_since
          FROM customer
          WHERE c_last = p_c_last
          ORDER BY c_first;
        DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = 1;

        SELECT COUNT(c_id) INTO namecnt FROM customer WHERE c_last = p_c_last;

        WHILE i < p_iters DO
          SET done = 0;
          OPEN c_byname;
          SET j = 0;
          WHILE j < (namecnt + 1) DIV 2 AND done = 0 DO
            FETCH c_byname INTO v_first, v_middle, v_id, v_street_1,
                  v_street_2, v_city, v_state, v_zip, v_phone, v_credit,
                  v_credit_lim, v_discount, v_balance, v_since;
            SET j = j + 1;
          END WHILE;
          CLOSE c_byname;
          SET i = i + 1;
        END WHILE;
      END)"))
    die("CREATE PROCEDURE failed", conn);

  mysql_close(conn);
  std::printf("Schema ready.\n");
}

void worker(const Config &cfg, int id) {
  MYSQL *conn = connect(cfg, kDb);
  unsigned seed = 12345u + id;
  std::string call_prefix =
      "CALL payment_cursor('";  // + name + "'," + iters + ")"

  while (!g_stop.load(std::memory_order_relaxed)) {
    seed = seed * 1103515245u + 12345u;
    std::string sql = call_prefix + last_name(seed % kLastNames) + "'," +
                      std::to_string(cfg.inner_iters) + ")";
    if (exec(conn, sql)) {
      g_calls.fetch_add(1, std::memory_order_relaxed);
      g_cursor_opens.fetch_add(cfg.inner_iters, std::memory_order_relaxed);
    } else {
      g_errors.fetch_add(1, std::memory_order_relaxed);
      std::fprintf(stderr, "worker %d: %s\n", id, mysql_error(conn));
      std::this_thread::sleep_for(std::chrono::seconds(1));
    }
  }
  mysql_close(conn);  // releases the per-connection retained memory
}

long mysqld_rss_kb() {
  // Locate mysqld; if several run, take the first one
  FILE *p = popen("pgrep -x mysqld | head -1", "r");
  if (!p) return -1;
  char buf[32] = {0};
  if (!fgets(buf, sizeof(buf), p)) {
    pclose(p);
    return -1;
  }
  pclose(p);
  long pid = std::atol(buf);
  if (pid <= 0) return -1;

  std::ifstream st("/proc/" + std::to_string(pid) + "/status");
  std::string line;
  while (std::getline(st, line))
    if (line.rfind("VmRSS:", 0) == 0)
      return std::atol(line.c_str() + 6);
  return -1;
}

void report_sample(MYSQL *conn, double elapsed_sec, const char *tag) {
  long rss = mysqld_rss_kb();
  std::string total = query_value(
      conn, "SELECT total_allocated FROM sys.memory_global_total");
  long long opens = g_cursor_opens.load();

  std::printf("[%7.1fs]%s rss=%.1f MB  instrumented=%s  opens=%lld  "
              "opens/s=%.0f  calls=%lld  errors=%lld\n",
              elapsed_sec, tag, rss / 1024.0, total.c_str(), opens,
              elapsed_sec > 0 ? opens / elapsed_sec : 0.0, g_calls.load(),
              g_errors.load());

  // Top server-side memory consumers — the cursor growth shows up here
  if (mysql_query(conn,
                  "SELECT event_name, current_number_of_bytes_used "
                  "FROM performance_schema.memory_summary_global_by_event_name "
                  "ORDER BY current_number_of_bytes_used DESC LIMIT 3") == 0) {
    if (MYSQL_RES *res = mysql_store_result(conn)) {
      MYSQL_ROW row;
      while ((row = mysql_fetch_row(res)))
        std::printf("            top: %-60s %12.1f MB\n", row[0],
                    std::atoll(row[1]) / 1048576.0);
      mysql_free_result(res);
    }
  }
  std::fflush(stdout);
}

}  // namespace

int main(int argc, char **argv) {
  Config cfg;
  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto val = [&](const char *k) -> const char * {
      size_t n = std::strlen(k);
      return a.compare(0, n, k) == 0 ? a.c_str() + n : nullptr;
    };
    if (const char *v = val("--socket=")) cfg.socket = v;
    else if (const char *v = val("--host=")) cfg.host = v;
    else if (const char *v = val("--port=")) cfg.port = std::atoi(v);
    else if (const char *v = val("--user=")) cfg.user = v;
    else if (const char *v = val("--password=")) cfg.password = v;
    else if (const char *v = val("--threads=")) cfg.threads = std::atoi(v);
    else if (const char *v = val("--duration=")) cfg.duration_sec = std::atoi(v);
    else if (const char *v = val("--report=")) cfg.report_sec = std::atoi(v);
    else if (const char *v = val("--inner-iters=")) cfg.inner_iters = std::atoi(v);
    else if (const char *v = val("--rows-per-name=")) cfg.rows_per_name = std::atoi(v);
    else if (a == "--skip-setup") cfg.skip_setup = true;
    else die("unknown argument: " + a + " (see header comment for usage)");
  }

  if (mysql_library_init(0, nullptr, nullptr)) die("mysql_library_init failed");

  if (!cfg.skip_setup) setup_schema(cfg);

  std::printf("Starting %d worker threads, %d cursor opens per CALL, "
              "%d seconds...\n\n",
              cfg.threads, cfg.inner_iters, cfg.duration_sec);

  MYSQL *mon = connect(cfg, kDb);
  auto start = std::chrono::steady_clock::now();
  auto elapsed = [&] {
    return std::chrono::duration<double>(std::chrono::steady_clock::now() -
                                         start).count();
  };

  report_sample(mon, 0.0, " [baseline]");

  std::vector<std::thread> workers;
  for (int i = 0; i < cfg.threads; ++i)
    workers.emplace_back(worker, std::cref(cfg), i);

  while (elapsed() < cfg.duration_sec) {
    std::this_thread::sleep_for(std::chrono::seconds(cfg.report_sec));
    report_sample(mon, elapsed(), "");
  }

  g_stop = true;
  for (auto &t : workers) t.join();

  report_sample(mon, elapsed(), " [workers stopped, still connected? no]");
  // Give the server a moment to tear down the disconnected sessions
  std::this_thread::sleep_for(std::chrono::seconds(3));
  report_sample(mon, elapsed(), " [after disconnect]");

  std::printf("\nDone. If rss grew steadily during the run and dropped at"
              " [after disconnect], the per-connection cursor retention is"
              " reproduced.\n");
  mysql_close(mon);
  mysql_library_end();
  return 0;
}
