# %%
# This script will test umodbus comms to a Solis Hybrid Inverter with a DLS-L LAN Data Logger
#
# It requires the umodbus module
#
# It will report the inverter temperaturem, the grid voltage and the battery state of charge

import socket
from umodbus import conf
from umodbus.client import tcp
from errno import ENETUNREACH

# configuration
CFG = {
    "S2-WL": {
        "IP": "<IP>",
        "PORT": 502,
    },
}  # Update with your inverter IP

devices = [
    'S2-WL',
]

START_REGISTRY = 3000
STOP_REGISTRY = 3100

# Define the initial JSON object
initial_json = {
    "addrs": START_REGISTRY,
    "len": 1,
}

# Specify the number of additional objects to generate
num_objects = STOP_REGISTRY - initial_json["addrs"] + 1

# List to store the generated JSON objects
payloads = [initial_json.copy()]

# Loop to increment addrs and create new objects
for i in range(1, num_objects):
    new_object = initial_json.copy()
    new_object["addrs"] += i
    payloads.append(new_object)

# Enable values to be signed (default is False).
conf.SIGNED_VALUES = False
sock = {}
for device in devices:
    try:
        sock[device] = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock[device].connect((CFG[device]["IP"], CFG[device]["PORT"]))
        print(f"Device: {device}")
        print("-" * 32 + "\n")
        for payload in payloads:
            try:
                message = tcp.read_input_registers(slave_id=1, starting_address=payload["addrs"], quantity=payload["len"])
                response = tcp.send_message(message, sock[device])
                #print("raw ", response[0])
                #val = float(response[0]) *<payload["scale"]
                #print(f"{payload['addrs']} {(payload['desc']+':'):25s}{val:5.1f} {payload['uom']} {'(raw: ' + str(response[0]) +')'}")
                print(f"{(str(payload['addrs'])+':'):25s}{response[0]}")
            except Exception as e:
                print(f"{(str(payload['addrs'])+':'):25s} error")
                continue

        print("\n\n")
        sock[device].close()
    except IOError as e:
        # an IOError exception occurred (socket.error is a subclass)
        if e.errno == ENETUNREACH:
            # now we had the error code 101, network unreachable
            print("could not connect")
        else:
            # other exceptions we reraise again
            raise

# %%
