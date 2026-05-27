#!/usr/bin/tclsh
# HammerDB 5.0 TPC-C schema build for MySQL 8.4.
# buildschema blocks until all loader virtual users finish (internal
# _waittocomplete) and then creates the TPC-C stored procedures, so no
# external wait is needed.
#
# Run via: /opt/HammerDB-5.0/hammerdbcli auto /root/benchmarks/hammerdb_load.tcl

puts "SETTING CONFIGURATION"
dbset db mysql
dbset bm TPC-C

diset connection localhost
#diset connection mysql_port 3306
diset connection mysql_socket /tmp/mysql-alloc-test.sock
diset connection mysql_ssl false

diset tpcc mysql_user tpcuser
diset tpcc mysql_pass tpcpass
diset tpcc mysql_dbase tpcc
diset tpcc mysql_storage_engine innodb
diset tpcc mysql_history_pk true
diset tpcc mysql_partition true

diset tpcc mysql_count_ware 3000
diset tpcc mysql_num_vu 80

# Drop and recreate the database to ensure it's empty
puts "DROPPING AND RECREATING DATABASE"
package require mysqltcl

set host "localhost"
set user "tpcuser"
set pass "tpcpass"
set socket "/tmp/mysql-alloc-test.sock"
set dbname "tpcc"

#set conn [mysqlconnect -host $host -socket $socket -user $user -password $pass]
#mysqlexec $conn "DROP DATABASE IF EXISTS $dbname"
#mysqlexec $conn "CREATE DATABASE $dbname"
#mysqlclose $conn
#puts "DATABASE $dbname READY"

# Patch the CreateTables procedure to fix version check
# Load the patched version from our modified source
puts "Loading patched MySQL procedures..."
source /home/bogdan.degtyariov/2026-allocators/mysqloltp.tcl

puts "SCHEMA BUILD STARTED"
set ret [buildschema]
puts "SCHEMA BUILD COMPLETED: $ret"
