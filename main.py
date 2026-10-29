#!/usr/bin/env python3
"""
main.py - CLI (curses/ASCII) stick and AUX channel visualizer
for CRSF packets (RC_CHANNELS_PACKED, frame type 0x16).

Data sources:
  --stdin                   read raw CRSF bytes from stdin (client-side tee pipeline)
  --unix-dgram PATH         bind a UNIX datagram socket (local IPC instead of UDP)
  --unix-stream PATH        connect to an existing UNIX stream socket as a client
  --udp HOST:PORT           listen for raw CRSF frames over UDP (default: 0.0.0.0:5555)
  --serial DEV --baud BAUD  read directly from UART (requires pyserial)

Example (client-side tee, alongside the main stream consumer):
  cat /dev/ttyUSB0 | tee >(python3 main.py --stdin) | main_consumer
  # Or, if the stream is already forwarded to another process over UDP:
  socat /dev/ttyUSB0,raw,b420000 - | tee >(python3 main.py --stdin) > /dev/udp/127.0.0.1/5555

  python3 main.py --udp 0.0.0.0:5555
  python3 main.py --serial /dev/ttyUSB0 --baud 420000

  # UNIX datagram: the forwarder sends directly to a socket path instead of UDP:
  python3 main.py --unix-dgram /tmp/crsf.sock

  # UNIX stream: socat already serves the socket with fork for multiple readers:
  socat UNIX-LISTEN:/tmp/crsf.sock,fork,reuseaddr /dev/ttyUSB0,raw,b420000 &
  python3 main.py --unix-stream /tmp/crsf.sock

In --stdin mode, curses opens keyboard input and screen output through /dev/tty
to avoid conflicting with the CRSF stream on fd0. Press Ctrl+C or 'q' to quit.
"""

import argparse
import curses
import os
import select
import socket
import sys
import time

# ---------------------------------------------------------------------------
# CRSF constants
# ---------------------------------------------------------------------------

CRSF_SYNC_BYTES = (0xC8, 0xEE, 0xEC, 0xEA)  # FC addr, TX-module, ext src variants
CRSF_FRAMETYPE_RC_CHANNELS_PACKED = 0x16
CRSF_MAX_FRAME_LEN = 64
NUM_CHANNELS = 16

# CRC8 DVB-S2 lookup table (poly 0xD5), used by CRSF
def _make_crc8_table(poly=0xD5):
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ poly) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
        table.append(crc)
    return table

CRC8_TABLE = _make_crc8_table()

def crc8_dvb_s2(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = CRC8_TABLE[crc ^ b]
    return crc


def unpack_channels(payload: bytes):
    """Unpack 22 bytes (16 channels of 11 bits each, little-endian bitstream)."""
    bits = 0
    bitcount = 0
    channels = []
    for byte in payload:
        bits |= byte << bitcount
        bitcount += 8
        while bitcount >= 11 and len(channels) < NUM_CHANNELS:
            channels.append(bits & 0x7FF)
            bits >>= 11
            bitcount -= 11
    return channels


def crsf_to_us(raw: int) -> int:
    """Convert CRSF raw values (172..1811, center 992) to microseconds (988..2012, center 1500)."""
    return int(round((raw - 992) * (1024.0 / 1639.0) + 1500))


def us_to_norm(us: int, lo=988, hi=2012):
    """Normalize to the range -1.0..1.0."""
    mid = (lo + hi) / 2.0
    span = (hi - lo) / 2.0
    return max(-1.0, min(1.0, (us - mid) / span))


# ---------------------------------------------------------------------------
# CRSF frame extraction from a rolling byte buffer
# ---------------------------------------------------------------------------

class CrsfFrameParser:
    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf.extend(data)

    def pop_frames(self):
        """Yield valid (frametype, payload) frames extracted from the buffer."""
        while True:
            # Find a sync byte.
            sync_pos = None
            for i, b in enumerate(self.buf):
                if b in CRSF_SYNC_BYTES:
                    sync_pos = i
                    break
            if sync_pos is None:
                self.buf.clear()
                return
            if sync_pos > 0:
                del self.buf[:sync_pos]

            if len(self.buf) < 3:
                return  # Wait for more data.

            length = self.buf[1]  # Includes type + payload + CRC.
            if length < 2 or length > CRSF_MAX_FRAME_LEN:
                # Invalid data after a sync byte; move on.
                del self.buf[0:1]
                continue

            full_len = 2 + length  # sync + len_byte + (type+payload+crc)
            if len(self.buf) < full_len:
                return  # The complete frame has not arrived yet.

            frame = bytes(self.buf[:full_len])
            del self.buf[:full_len]

            frametype = frame[2]
            payload_and_crc = frame[2:-1]
            crc_received = frame[-1]
            crc_calc = crc8_dvb_s2(payload_and_crc)
            if crc_calc != crc_received:
                continue  # Skip a corrupted frame.

            payload = frame[3:-1]
            yield frametype, payload


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

class UdpSource:
    def __init__(self, host, port):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        # Increase the OS receive buffer to absorb brief processing delays
        # at 250 Hz (26 bytes * 250 is about 6.5 KB/s). Traffic volume is low;
        # the buffer only needs to absorb jitter.
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 16)
        except OSError:
            pass
        self.sock.bind((host, port))

    def read(self, timeout=0.02):
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return b""
        # recvfrom() returns exactly one datagram per call. At 250 Hz
        # (one frame every 4 ms), redraws or GC pauses can leave several
        # frames queued between select() calls. Drain the entire queue in
        # one pass so the display shows the latest stick positions.
        chunks = []
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
                chunks.append(data)
            except BlockingIOError:
                break
        return b"".join(chunks)

    def close(self):
        self.sock.close()


class UnixDgramSource:
    """Preserve datagram boundaries without the network stack overhead.

    Loopback UDP still uses the kernel's TCP/IP path; AF_UNIX does not.
    A forwarder can replace sendto(('127.0.0.1', 5555)) with a sendto()
    call targeting '/tmp/crsf.sock'.
    """

    def __init__(self, path):
        self.path = path
        # Remove a stale socket file from a previous run; bind() fails
        # if the file already exists.
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sock.bind(path)
        try:
            os.chmod(path, 0o666)  # Allow a forwarder with another UID to write.
        except OSError:
            pass

    def read(self, timeout=0.02):
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return b""
        # Drain all queued datagrams in one read(), as in UdpSource.
        chunks = []
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
                chunks.append(data)
            except BlockingIOError:
                break
        return b"".join(chunks)

    def close(self):
        self.sock.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


class UnixStreamSource:
    """Connect as a client to an existing UNIX stream socket, for example:
    socat UNIX-LISTEN:/tmp/crsf.sock,fork,reuseaddr /dev/ttyUSB0,raw,b420000

    Forking for each connection lets several independent readers share
    one socket, like tee over a socket.
    """

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)  # Connecting to a local socket is immediate.
        self.sock.setblocking(False)

    def read(self, timeout=0.02):
        r, _, _ = select.select([self.sock], [], [], timeout)
        if not r:
            return b""
        try:
            data = self.sock.recv(4096)
        except BlockingIOError:
            return b""
        if not data:
            raise EOFError("unix stream socket closed by peer")
        return data

    def close(self):
        self.sock.close()


class StdinSource:
    """Read raw CRSF bytes from fd0, typically split by client-side tee.

    Example: serial/socat -> tee -> [this script, main consumer].
    """

    def __init__(self):
        self.fd = sys.stdin.fileno()

    def read(self, timeout=0.02):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if r:
            try:
                data = os.read(self.fd, 4096)
            except OSError:
                return b""
            if not data:
                # EOF: the upstream tee producer has closed.
                raise EOFError("stdin closed (upstream tee/producer exited)")
            return data
        return b""

    def close(self):
        pass


class SerialSource:
    def __init__(self, dev, baud):
        import serial  # Import locally so UDP mode does not require pyserial.
        self.ser = serial.Serial(dev, baudrate=baud, timeout=0)

    def read(self, timeout=0.02):
        time.sleep(timeout)
        n = self.ser.in_waiting
        if n:
            return self.ser.read(n)
        return b""

    def close(self):
        self.ser.close()


# ---------------------------------------------------------------------------
# ASCII rendering helpers
# ---------------------------------------------------------------------------

STICK_W = 21   # Odd width provides a center cell.
STICK_H = 11

def render_stick_box(win, y0, x0, xnorm, ynorm, label):
    """Draw a STICK_W x STICK_H box with the stick position marked inside."""
    # Border.
    win.addstr(y0, x0, "+" + "-" * (STICK_W - 2) + "+")
    for row in range(1, STICK_H - 1):
        win.addstr(y0 + row, x0, "|" + " " * (STICK_W - 2) + "|")
    win.addstr(y0 + STICK_H - 1, x0, "+" + "-" * (STICK_W - 2) + "+")

    # Dashed center cross marks the neutral position.
    cx = x0 + STICK_W // 2
    cy = y0 + STICK_H // 2
    for row in range(1, STICK_H - 1):
        try:
            win.addch(y0 + row, cx, ord(':'))
        except curses.error:
            pass
    for col in range(1, STICK_W - 1):
        try:
            win.addch(cy, x0 + col, ord('-'))
        except curses.error:
            pass

    # Stick coordinates are in [-1..1]; invert Y so positive values point up.
    px = int(round((xnorm + 1) / 2 * (STICK_W - 3))) + x0 + 1
    py = int(round((1 - ynorm) / 2 * (STICK_H - 3))) + y0 + 1
    px = max(x0 + 1, min(x0 + STICK_W - 2, px))
    py = max(y0 + 1, min(y0 + STICK_H - 2, py))
    try:
        win.addch(py, px, ord('O'), curses.A_BOLD)
    except curses.error:
        pass

    win.addstr(y0 + STICK_H, x0, label.center(STICK_W))


def render_bar(win, y, x, width, norm01, label, value_str):
    """Draw a horizontal bar with norm01 in [0,1]."""
    filled = int(round(norm01 * width))
    bar = "#" * filled + "-" * (width - filled)
    win.addstr(y, x, f"{label:>5} [{bar}] {value_str}")


def render_switch(win, y, x, raw_us, label):
    """Show an AUX channel bar and its approximate switch position (1/2/3)."""
    n = us_to_norm(raw_us)
    if n < -0.35:
        pos = "LOW "
    elif n > 0.35:
        pos = "HIGH"
    else:
        pos = "MID "
    norm01 = (n + 1) / 2
    render_bar(win, y, x, 20, norm01, label, f"{raw_us}us {pos}")


# ---------------------------------------------------------------------------
# Main curses loop
# ---------------------------------------------------------------------------

def main(stdscr, source, args):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(20)

    parser = CrsfFrameParser()
    channels = [992] * NUM_CHANNELS
    frames_total = 0
    last_frame_ts = 0.0
    fps_counter = 0
    fps = 0.0
    fps_ts = time.time()

    # The terminal does not need to refresh at the CRSF input rate (250 Hz).
    # Redrawing with erase()+addstr() on every iteration wastes CPU.
    # Parse frames at full speed and throttle redraws with a separate timer.
    RENDER_HZ = 30.0
    RENDER_INTERVAL = 1.0 / RENDER_HZ
    last_render = 0.0

    while True:
        # Quit on 'q'.
        try:
            key = stdscr.getch()
            if key in (ord('q'), ord('Q'), 27):
                break
        except curses.error:
            pass

        try:
            data = source.read(timeout=0.005)
        except EOFError:
            break

        if data:
            parser.feed(data)
            for frametype, payload in parser.pop_frames():
                if frametype == CRSF_FRAMETYPE_RC_CHANNELS_PACKED and len(payload) >= 22:
                    channels = unpack_channels(payload[:22])
                    frames_total += 1
                    fps_counter += 1
                    last_frame_ts = time.time()

        now = time.time()
        if now - fps_ts >= 1.0:
            fps = fps_counter / (now - fps_ts)
            fps_counter = 0
            fps_ts = now

        if now - last_render < RENDER_INTERVAL:
            continue  # Save CPU until the next redraw is due.
        last_render = now

        stdscr.erase()
        h, w = stdscr.getmaxyx()

        title = " CRSF Stick / AUX Visualizer "
        stdscr.addstr(0, max(0, (w - len(title)) // 2), title, curses.A_REVERSE)

        age = now - last_frame_ts if last_frame_ts else None
        status = f"frames: {frames_total}  fps: {fps:5.1f}  "
        status += "link: OK" if (age is not None and age < 0.5) else "link: NO SIGNAL"
        stdscr.addstr(1, 2, status, curses.A_BOLD if (age is not None and age < 0.5) else curses.A_DIM)

        us = [crsf_to_us(c) for c in channels]

        # Mode 2: CH1=Roll, CH2=Pitch, CH3=Throttle, CH4=Yaw (standard CRSF mapping).
        roll_n = us_to_norm(us[0])
        pitch_n = us_to_norm(us[1])
        throttle_n = us_to_norm(us[2])
        yaw_n = us_to_norm(us[3])

        y0 = 3
        left_x = 4
        right_x = left_x + STICK_W + 8

        render_stick_box(stdscr, y0, left_x, yaw_n, throttle_n, "THR / YAW (L)")
        render_stick_box(stdscr, y0, right_x, roll_n, pitch_n, "ROLL / PITCH (R)")

        # Numeric values of the primary channels.
        info_y = y0 + STICK_H + 2
        stdscr.addstr(info_y, left_x,
                      f"CH1 Roll:{us[0]:5d}us  CH2 Pitch:{us[1]:5d}us  "
                      f"CH3 Thr:{us[2]:5d}us  CH4 Yaw:{us[3]:5d}us")

        # AUX channels CH5..CH16.
        aux_y = info_y + 2
        stdscr.addstr(aux_y, left_x, "AUX channels:", curses.A_UNDERLINE)
        for i in range(4, NUM_CHANNELS):
            row = aux_y + 1 + (i - 4)
            if row >= h - 1:
                break
            render_switch(stdscr, row, left_x, us[i], f"AUX{i-3}")

        footer = "q - quit"
        if h > 0:
            stdscr.addstr(min(h - 1, aux_y + 1 + (NUM_CHANNELS - 4) + 1), left_x, footer, curses.A_DIM)

        stdscr.refresh()


def run_with_tty(source, args):
    """Run curses through /dev/tty, leaving fd0 free for CRSF bytes from tee."""
    tty = open("/dev/tty", "r+b", buffering=0)
    term_type = os.environ.get("TERM", "xterm")
    screen = curses.newterm(term_type, tty.fileno(), tty.fileno())
    curses.set_term(screen)
    stdscr = curses.initscr()
    curses.noecho()
    curses.cbreak()
    stdscr.keypad(True)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        main(stdscr, source, args)
    finally:
        stdscr.keypad(False)
        curses.echo()
        curses.nocbreak()
        curses.endwin()
        tty.close()


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stdin", action="store_true",
                     help="read raw CRSF bytes from stdin (client-side tee pipeline)")
    ap.add_argument("--unix-dgram", metavar="PATH", default=None,
                     help="bind a UNIX datagram socket at PATH (local IPC instead of UDP)")
    ap.add_argument("--unix-stream", metavar="PATH", default=None,
                     help="connect as a client to an existing UNIX stream socket at PATH "
                          "(for example, socat UNIX-LISTEN:PATH,fork)")
    ap.add_argument("--udp", metavar="HOST:PORT", default="0.0.0.0:5555",
                     help="listen for raw CRSF frames over UDP (default: 0.0.0.0:5555)")
    ap.add_argument("--serial", metavar="DEV", default=None,
                     help="read directly from UART, for example /dev/ttyUSB0 (requires pyserial)")
    ap.add_argument("--baud", type=int, default=420000, help="UART baud rate (default: 420000)")
    return ap.parse_args()


def _make_source(args):
    if args.stdin:
        return StdinSource()
    if args.unix_dgram:
        return UnixDgramSource(args.unix_dgram)
    if args.unix_stream:
        try:
            return UnixStreamSource(args.unix_stream)
        except FileNotFoundError:
            sys.exit(f"error: socket '{args.unix_stream}' does not exist "
                     f"(is the server/socat running?)")
        except ConnectionRefusedError:
            sys.exit(f"error: '{args.unix_stream}' exists, but no server is listening "
                     f"(stale socket file? remove it and restart the server)")
    if args.serial:
        return SerialSource(args.serial, args.baud)
    host, port = args.udp.split(":")
    return UdpSource(host, int(port))


if __name__ == "__main__":
    args = parse_args()
    source = _make_source(args)

    try:
        if args.stdin:
            run_with_tty(source, args)
        else:
            curses.wrapper(main, source, args)
    except KeyboardInterrupt:
        pass
    finally:
        source.close()
