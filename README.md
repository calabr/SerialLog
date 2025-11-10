# SerialLog
Simple sensor request and logging app
Pyton application to request some data from sensor network oner serial interface with logging and graffical represention of received data.
Program can run as text-only aplication with lofgging sensor replys in terminal window, save CVS-like log and draw set of graps for requested data.

Date request parameters and representation can be configured by commandline parameters.
Applicateion tested with Python 3.9 in Windows environment.

Application code created with aid of ChatGPT.

# Serial Polling Tool

A Python script for polling microcontroller devices over a serial port, logging cell data, and displaying results in real-time.

## Features

- Polls specified cells or sends a custom request string.
- Configurable serial port, baud rate, poll interval, and delay between requests.
- Parses responses in the format `$<cell>:<value>,CRC8\n`.
- Supports multiple responses in one line and ignores CRC.
- Logs results to a CSV-like file, grouped by polling cycle.
- Displays timestamps in milliseconds since program start.
- Initial 2-second startup prints all incoming data raw.
- Missing responses are logged as `0`.
- Debug mode prints unrecognized responses.

## Requirements

- Python 3.x
- `pyserial` library

Install `pyserial` if needed:

```bash
pip install pyserial
```
Usage
python poll_serial.py -p <port> [options] [cell numbers]

## Command-line Parameters

   -p <port> — Serial port (required)
   -s <baud> — Port speed (default 115200)
   -t <ms> — Poll interval in milliseconds (default 1000)
   -w <ms> — Delay between cell requests in milliseconds (default 20)
   -f <filename> — Optional output log file
   -? "<string>" — Custom request string (overrides cell list)
   -d — Debug mode: print unparsed responses
   cells — List of cell numbers (1–255)

## Examples

Poll cells 1, 2, 3 every 2 seconds, log to data.log:

  python poll_serial.py -p COM3 -t 2000 -f data.log 1 2 3


Send a custom request string:

  python poll_serial.py -p /dev/ttyUSB0 -? "?42\n"

Enable debug mode:

  python poll_serial.py -p COM3 -d 1 2 3

## Output

Console:
  Shows real-time values per cell with timestamp.

Log file:

With cells: Time_ms, Cell1, Cell2, ...

With custom request: Time_ms, Values
One line per polling cycle.
Missing responses logged as 0.

## Exit

Press Ctrl+C to stop polling gracefully.
