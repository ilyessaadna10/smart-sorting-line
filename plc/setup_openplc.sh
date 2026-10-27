#!/bin/bash
# Load the sorting-line program into a local OpenPLC_v3 install and register the
# line simulator as a Modbus TCP remote I/O device. Usage: ./setup_openplc.sh /path/to/OpenPLC_v3
set -e
OPENPLC="${1:?path to OpenPLC_v3}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$OPENPLC/webserver"

cp "$HERE/sorting_line.st" st_files/sorting_line.st
sqlite3 openplc.db <<'SQL'
DELETE FROM Programs WHERE File = 'sorting_line.st';
INSERT INTO Programs (Name, Description, File, Date_upload)
  VALUES ('Smart sorting line', 'Conveyor + vision + ejector with part tracking', 'sorting_line.st', strftime('%s','now'));
DELETE FROM Slave_dev WHERE dev_name = 'line_remote_io';
INSERT INTO Slave_dev (dev_name, dev_type, slave_id, com_port, baud_rate, parity, data_bits, stop_bits,
                       ip_address, ip_port, di_start, di_size, coil_start, coil_size, ir_start, ir_size,
                       hr_read_start, hr_read_size, hr_write_start, hr_write_size, pause)
  VALUES ('line_remote_io', 'TCP', 1, '', 115200, 'None', 8, 1,
          '127.0.0.1', 5020, 0, 16, 0, 8, 0, 1, 0, 0, 0, 0, 0);
UPDATE Settings SET Value = '5502' WHERE Key = 'Modbus_port';      -- unprivileged port, no root needed
UPDATE Settings SET Value = '20'   WHERE Key = 'Slave_polling';    -- poll the remote I/O every 20 ms
UPDATE Settings SET Value = 'true' WHERE Key = 'Start_run_mode';   -- start in RUN mode
SQL

# mbconfig.cfg is what the runtime reads for remote devices (same format the web UI writes)
cat > mbconfig.cfg <<'CFG'
Num_Devices = "1"
Polling_Period = "20"
Timeout = "1000"
# ------------
#   DEVICE 0
# ------------
device0.name = "line_remote_io"
device0.slave_id = "1"
device0.protocol = "TCP"
device0.address = "127.0.0.1"
device0.IP_Port = "5020"
device0.RTU_Baud_Rate = "115200"
device0.RTU_Parity = "None"
device0.RTU_Data_Bits = "8"
device0.RTU_Stop_Bits = "1"
device0.RTU_TX_Pause = "0"

device0.Discrete_Inputs_Start = "0"
device0.Discrete_Inputs_Size = "16"
device0.Coils_Start = "0"
device0.Coils_Size = "8"
device0.Input_Registers_Start = "0"
device0.Input_Registers_Size = "1"
device0.Holding_Registers_Read_Start = "0"
device0.Holding_Registers_Read_Size = "0"
device0.Holding_Registers_Start = "0"
device0.Holding_Registers_Size = "0"
CFG

./scripts/compile_program.sh sorting_line.st
echo "OpenPLC ready. Start it with: $OPENPLC/start_openplc.sh   (web UI http://localhost:8080, login openplc/openplc)"
