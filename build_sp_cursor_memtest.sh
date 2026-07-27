#!/bin/bash
set -euo pipefail

cd /home/bogdan.degtyariov/2026-allocators-old
S=/home/bogdan.degtyariov/servers/Percona-Server-8.4.8-8-Linux.x86_64.glibc2.35
g++ -O2 -std=c++17 -o sp_cursor_memtest sp_cursor_memtest.cpp -I$S/include -L$S/lib -lperconaserverclient -lpthread -Wl,-rpath,$S/lib && echo BUILD_OK
