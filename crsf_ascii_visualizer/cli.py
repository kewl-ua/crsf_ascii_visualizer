#!/usr/bin/env python3
"""Visualize 16 CRSF RC channels as sticks and AUX switches in a terminal.

Examples:
  crsf-viz --udp 0.0.0.0:5555
  crsf-viz --serial /dev/ttyUSB0 --baud 420000
  crsf-viz --unix-dgram /tmp/crsf.sock
  crsf-viz --unix-stream /tmp/crsf.sock
  cat frames.bin | crsf-viz --stdin

Input must contain raw CRSF RC_CHANNELS_PACKED (0x16) frames.
Press q, Esc or Ctrl+C to quit. Use a terminal of at least 80 columns by 33 rows.
"""

import argparse
from contextlib import ExitStack
import errno
import os
import select
import socket
import stat
import sys
import time

from . import __version__


# Load curses only for interactive operation; help and version need no terminal.
curses = None
DEFAULT_UDP = "0.0.0.0:5555"
MIN_COLS = 80
MIN_ROWS = 33


def _load_curses():
    global curses
    if curses is None:
        import curses as curses_module
        curses = curses_module
    return curses

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
        try:
            self.sock.bind((host, port))
        except OSError:
            self.sock.close()
            raise

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
        self._path_identity = None
        # Never remove an existing file, symlink or another process's socket.
        if os.path.lexists(path):
            raise FileExistsError(errno.EEXIST,
                                  "socket path already exists; choose another path or remove a stale socket",
                                  path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        try:
            self.sock.bind(path)
        except OSError:
            self.sock.close()
            raise
        try:
            owned = os.stat(path, follow_symlinks=False)
        except OSError:
            self.sock.close()
            raise
        self._path_identity = (owned.st_dev, owned.st_ino)
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
        identity, self._path_identity = self._path_identity, None
        if identity is None:
            return
        try:
            current = os.stat(self.path, follow_symlinks=False)
            if stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == identity:
                os.unlink(self.path)
        except FileNotFoundError:
            pass


class UnixStreamSource:
    """Connect as a client to an existing UNIX stream socket, for example:
    socat UNIX-LISTEN:/tmp/crsf.sock,fork,reuseaddr /dev/ttyUSB0,raw,b420000

    Forking for each connection lets several independent readers share
    one socket, like tee over a socket.
    """

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(path)  # Connecting to a local socket is immediate.
        except OSError:
            self.sock.close()
            raise
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

    def __init__(self, fd=None):
        # curses needs fd0 for keyboard input; retain a separate stream reader.
        self.fd = os.dup(sys.stdin.fileno() if fd is None else fd)

    def read(self, timeout=0.02):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if r:
            data = os.read(self.fd, 4096)
            if not data:
                # EOF: the upstream tee producer has closed.
                raise EOFError("stdin closed (upstream tee/producer exited)")
            return data
        return b""

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


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
    _load_curses()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
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

        if h < MIN_ROWS or w < MIN_COLS:
            message = f"Resize terminal to at least {MIN_COLS} columns x {MIN_ROWS} rows; q to quit."
            if h > 0 and w > 1:
                try:
                    stdscr.addstr(0, 0, message[:w - 1])
                except curses.error:
                    pass
            stdscr.refresh()
            continue

        try:
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
        except curses.error:
            # A resize may change the available cells between size check and draw.
            continue


def run_with_tty(source, args):
    """Use the controlling tty while source retains the duplicated stdin fd.

    Python's curses exposes wrapper(), not newterm()/set_term(). Temporarily
    attach the standard descriptors used by ncurses to /dev/tty, then restore
    them even when initialization, drawing or input raises an exception.
    """
    _load_curses()
    with ExitStack() as cleanup:
        tty_fd = os.open("/dev/tty", os.O_RDWR)
        cleanup.callback(os.close, tty_fd)
        sys.stdout.flush()
        saved_stdin = os.dup(0)
        cleanup.callback(os.close, saved_stdin)
        cleanup.callback(os.dup2, saved_stdin, 0)
        saved_stdout = os.dup(1)
        cleanup.callback(os.close, saved_stdout)
        cleanup.callback(os.dup2, saved_stdout, 1)
        os.dup2(tty_fd, 0)
        os.dup2(tty_fd, 1)
        try:
            curses.wrapper(main, source, args)
        finally:
            # Flush while stdout still points to the tty, before restoring a pipe.
            sys.stdout.flush()


def _udp_endpoint(value):
    if value.count(":") != 1:
        raise argparse.ArgumentTypeError("use HOST:PORT, for example 0.0.0.0:5555")
    host, port_text = value.rsplit(":", 1)
    if not host or not port_text.isascii() or not port_text.isdecimal():
        raise argparse.ArgumentTypeError("use a nonempty HOST and a numeric PORT")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("PORT must be between 1 and 65535")
    return value


def _positive_baud(value):
    try:
        baud = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("baud rate must be a positive integer") from None
    if baud <= 0:
        raise argparse.ArgumentTypeError("baud rate must be a positive integer")
    return baud


def _nonempty_path(value):
    if not value or "\0" in value:
        raise argparse.ArgumentTypeError("provide a nonempty device or socket path")
    return value


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="crsf-viz", description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sources = ap.add_mutually_exclusive_group()
    sources.add_argument("--stdin", action="store_true",
                     help="read raw CRSF bytes from stdin (client-side tee pipeline)")
    sources.add_argument("--unix-dgram", metavar="PATH", type=_nonempty_path,
                     help="bind a UNIX datagram socket at PATH (local IPC instead of UDP)")
    sources.add_argument("--unix-stream", metavar="PATH", type=_nonempty_path,
                     help="connect as a client to an existing UNIX stream socket at PATH "
                          "(for example, socat UNIX-LISTEN:PATH,fork)")
    sources.add_argument("--udp", metavar="HOST:PORT", type=_udp_endpoint,
                     help="listen for raw CRSF frames over UDP (default: 0.0.0.0:5555)")
    sources.add_argument("--serial", metavar="DEV", type=_nonempty_path,
                     help="read directly from UART, for example /dev/ttyUSB0 (requires pyserial)")
    ap.add_argument("--baud", type=_positive_baud, default=420000,
                    help="UART baud rate (default: 420000)")
    args = ap.parse_args(argv)
    if not any((args.stdin, args.unix_dgram, args.unix_stream, args.udp, args.serial)):
        args.udp = DEFAULT_UDP
    return args


def _make_source(args):
    if args.stdin:
        return StdinSource()
    if args.unix_dgram:
        return UnixDgramSource(args.unix_dgram)
    if args.unix_stream:
        return UnixStreamSource(args.unix_stream)
    if args.serial:
        return SerialSource(args.serial, args.baud)
    host, port = args.udp.split(":")
    return UdpSource(host, int(port))


def run(argv=None):
    """Run the CLI and return a shell exit status."""
    args = parse_args(argv)
    source = None
    try:
        if os.name != "posix":
            raise RuntimeError("interactive mode requires Linux or another POSIX terminal")
        _load_curses()
        if not args.stdin and (not sys.stdin.isatty() or not sys.stdout.isatty()):
            raise RuntimeError("an interactive terminal is required; use --stdin for piped CRSF input")
        source = _make_source(args)
        if args.stdin:
            run_with_tty(source, args)
        else:
            curses.wrapper(main, source, args)
    except KeyboardInterrupt:
        return 130
    except ImportError as error:
        if args.serial and error.name == "serial":
            message = "serial input requires pyserial; install crsf-ascii-visualizer[serial]"
        else:
            message = "curses support is unavailable; install Python with ncurses support"
        print(f"crsf-viz: error: {message}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError) as error:
        if args.unix_stream and isinstance(error, FileNotFoundError):
            message = f"UNIX stream socket '{args.unix_stream}' does not exist; start the server first"
        elif args.unix_stream and isinstance(error, ConnectionRefusedError):
            message = f"no server is listening on '{args.unix_stream}'; check the server or stale socket path"
        elif args.stdin and getattr(error, "filename", None) == "/dev/tty":
            message = "stdin mode requires a controlling terminal (/dev/tty is unavailable)"
        else:
            message = str(error)
        print(f"crsf-viz: error: {message}", file=sys.stderr)
        return 1
    except Exception as error:
        if curses is None or not isinstance(error, curses.error):
            raise
        print(f"crsf-viz: error: cannot initialize or update the terminal: {error}", file=sys.stderr)
        return 1
    finally:
        if source is not None:
            try:
                source.close()
            except OSError as error:
                print(f"crsf-viz: warning: source cleanup failed: {error}", file=sys.stderr)
    return 0
