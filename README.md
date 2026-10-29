# CRSF ASCII Visualizer

![CRSF sticks and AUX channels](docs/assets/crsf-terminal.gif)

*Demo: synthetic CRSF input at 250 Hz.*

## Inspect sticks and AUX over UDP

![CRSF over UDP: forwarder, socket, decoder and terminal](docs/assets/crsf-udp-path.gif)

```bash
python3 main.py --udp 0.0.0.0:5555
```

## Read CRSF over UART

![CRSF over USB-UART: serial input, decoding and terminal output](docs/assets/crsf-signal-path.gif)

```bash
python3 -m pip install pyserial
python3 main.py --serial /dev/ttyUSB0 --baud 420000
```

## Listen on a UNIX datagram socket

![CRSF over UNIX datagrams: local forwarder and listening app](docs/assets/crsf-unix-dgram-path.gif)

```bash
python3 main.py --unix-dgram /tmp/crsf.sock
```

## Attach to an existing UNIX stream

![CRSF over UNIX stream: existing server and app client](docs/assets/crsf-unix-stream-path.gif)

```bash
python3 main.py --unix-stream /tmp/crsf.sock
```
