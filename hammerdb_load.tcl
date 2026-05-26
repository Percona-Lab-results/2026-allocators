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
diset connection mysql_port 3306
diset connection mysql_socket null

set hdb_ssl [expr {[info exists ::env(HDB_SSL)] ? $::env(HDB_SSL) : "true"}]
diset connection mysql_ssl $hdb_ssl
if {$hdb_ssl eq "true"} {
    set capath [expr {[info exists ::env(HDB_SSL_CAPATH)] ? $::env(HDB_SSL_CAPATH) : "/root/mysql-certs"}]
    set ca     [expr {[info exists ::env(HDB_SSL_CA)]     ? $::env(HDB_SSL_CA)     : "ca.pem"}]
    diset connection mysql_ssl_linux_capath $capath
    diset connection mysql_ssl_ca $ca
    diset connection mysql_ssl_cert ""
    diset connection mysql_ssl_key ""
    diset connection mysql_ssl_two_way false
}

diset tpcc mysql_user root
diset tpcc mysql_pass password
diset tpcc mysql_dbase tpcc
diset tpcc mysql_storage_engine innodb
diset tpcc mysql_history_pk true
diset tpcc mysql_partition true

diset tpcc mysql_count_ware 3000
diset tpcc mysql_num_vu 80

# Drop and recreate the database to ensure it's empty
puts "DROPPING AND RECREATING DATABASE"
package require mysqltcl

set host "db-bogdan-mysql-nyc1-002-8uod6.db1.ondigitalocean.com"
set port 3306
set user "benchmarkuser"
set pass "M3P6jA4k15bhwqWo0Nsl27Ze"
set dbname "tpcc"

#set conn [mysqlconnect -host $host -port $port -user $user -password $pass -ssl 1 -sslca "/root/mysql-certs/ca.pem"]
#mysqlexec $conn "DROP DATABASE IF EXISTS $dbname"
#mysqlexec $conn "CREATE DATABASE $dbname"
#mysqlclose $conn
#puts "DATABASE $dbname READY"

# Patch the CreateTables procedure to fix version check
# Load the patched version from our modified source
puts "Loading patched MySQL procedures..."
source /root/HammerDB-master/src/mysql/mysqloltp.tcl

puts "SCHEMA BUILD STARTED"
set ret [buildschema]
puts "SCHEMA BUILD COMPLETED: $ret"
