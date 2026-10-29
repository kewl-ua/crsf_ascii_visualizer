# CRSF ASCII Visualizer

![CRSF sticks and AUX channels](docs/assets/crsf-terminal.gif)

*Demo: synthetic CRSF input at 250 Hz.*

## Install the Linux command

Download the `.deb` from the [latest release](https://github.com/kewl-ua/crsf_ascii_visualizer/releases/latest), then:

```bash
sudo apt install ./crsf-ascii-visualizer_0.1.0_all.deb
crsf-viz --version
man crsf-viz
```

Or install from a source checkout without root:

```bash
make install PREFIX="$HOME/.local"
export PATH="$HOME/.local/bin:$PATH"
man -l "$HOME/.local/share/man/man1/crsf-viz.1"
```

## Inspect sticks and AUX over UDP

![CRSF over UDP: forwarder, socket, decoder and terminal](docs/assets/crsf-udp-path.gif)

```bash
crsf-viz --udp 0.0.0.0:5555
```

## Read CRSF over UART

Requires pyserial (`python3-serial` on Debian/Ubuntu).

![CRSF over USB-UART: serial input, decoding and terminal output](docs/assets/crsf-signal-path.gif)

```bash
crsf-viz --serial /dev/ttyUSB0 --baud 420000
```

## Listen on a UNIX datagram socket

![CRSF over UNIX datagrams: local forwarder and listening app](docs/assets/crsf-unix-dgram-path.gif)

```bash
crsf-viz --unix-dgram /tmp/crsf.sock
```

## Attach to an existing UNIX stream

![CRSF over UNIX stream: existing server and app client](docs/assets/crsf-unix-stream-path.gif)

```bash
crsf-viz --unix-stream /tmp/crsf.sock
```
