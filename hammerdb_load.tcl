#!/usr/bin/tclsh
# HammerDB 6.0 TPC-C schema build for MySQL 8.4.
# buildschema blocks until all loader virtual users finish (internal
# _waittocomplete) and then creates the TPC-C stored procedures, so no
# external wait is needed.
#
# Run via: /opt/HammerDB-6.0/hammerdbcli auto /root/benchmarks/hammerdb_load.tcl

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
# diset tpcc mysql_count_ware 10
# diset tpcc mysql_num_vu 10

puts "SCHEMA BUILD STARTED"
set ret [buildschema]
puts "SCHEMA BUILD COMPLETED: $ret"
