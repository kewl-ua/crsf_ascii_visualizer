"""Linux/POSIX integration against the real curses application and a PTY."""

import os
from pathlib import Path
import select
import signal
import socket
import sys
import time
import unittest

from test_protocol import make_frame

if os.name == "posix":
    import fcntl
    import pty
    import struct
    import termios


ROOT = Path(__file__).resolve().parents[1]


class TerminalProcess:
    """A child with a controlling terminal, optionally fed by a separate pipe."""
    def __init__(self, arguments, pipe_stdin=False, rows=34, columns=88):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, columns, 0, 0))
        pipe_read, pipe_write = os.pipe() if pipe_stdin else (-1, -1)
        child = os.fork()
        if child == 0:
            try:
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
                for target in (0, 1, 2):
                    os.dup2(slave, target)
                if pipe_stdin:
                    os.dup2(pipe_read, 0)
                for descriptor in (master, slave, pipe_read, pipe_write):
                    if descriptor > 2:
                        os.close(descriptor)
                os.chdir(ROOT)
                environment = dict(os.environ, TERM="xterm-256color",
                                   LC_ALL="C.UTF-8", PYTHONUNBUFFERED="1")
                os.execvpe(sys.executable,
                           [sys.executable, "-m", "crsf_ascii_visualizer", *arguments],
                           environment)
            except BaseException as error:
                os.write(2, repr(error).encode("utf-8", "replace"))
                os._exit(127)
        os.close(slave)
        if pipe_read >= 0:
            os.close(pipe_read)
        os.set_blocking(master, False)
        self.pid, self.master, self.pipe_write = child, master, pipe_write
        self.returncode = None
        self.output = bytearray()

    def poll(self):
        if self.returncode is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def read(self, timeout=0.05):
        ready, _, _ = select.select([self.master], [], [], timeout)
        if ready:
            try:
                data = os.read(self.master, 65536)
            except (OSError, BlockingIOError):
                data = b""
            self.output.extend(data)
        return bytes(self.output)

    def wait_for(self, text, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read()
            if text in self.output:
                return True
            if self.poll() is not None:
                break
        return False

    def wait(self, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.read()
            if self.poll() is not None:
                return self.returncode
        raise TimeoutError("curses process did not exit")

    def close(self):
        if self.pipe_write >= 0:
            os.close(self.pipe_write)
            self.pipe_write = -1
        if self.poll() is None:
            os.kill(self.pid, signal.SIGTERM)
            try:
                self.wait(timeout=3)
            except TimeoutError:
                os.kill(self.pid, signal.SIGKILL)
                self.wait(timeout=3)
        os.close(self.master)


@unittest.skipUnless(os.name == "posix", "PTY integration requires POSIX")
class TerminalTests(unittest.TestCase):
    def assert_terminal_success(self, process):
        os.write(process.master, b"q")
        self.assertEqual(process.wait(), 0, bytes(process.output).decode("utf-8", "replace"))
        self.assertNotIn(b"Traceback", process.output)

    def test_udp_frames_reach_real_curses_screen_and_q_exits(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        process = TerminalProcess(["--udp", f"127.0.0.1:{port}"])
        self.addCleanup(process.close)
        self.assertTrue(process.wait_for(b"CRSF Stick"), bytes(process.output))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            deadline = time.monotonic() + 5
            while b"2012" not in process.output and time.monotonic() < deadline:
                sender.sendto(make_frame([1811] * 16), ("127.0.0.1", port))
                process.read()
        self.assertIn(b"2012", process.output)
        self.assert_terminal_success(process)

    def test_stdin_binary_pipe_keeps_keyboard_on_controlling_tty(self):
        process = TerminalProcess(["--stdin"], pipe_stdin=True)
        self.addCleanup(process.close)
        self.assertTrue(process.wait_for(b"CRSF Stick"), bytes(process.output))
        os.write(process.pipe_write, make_frame([1811] * 16))
        self.assertTrue(process.wait_for(b"2012"), bytes(process.output))
        self.assert_terminal_success(process)

    def test_small_terminal_shows_resize_hint_and_can_quit(self):
        process = TerminalProcess(["--stdin"], pipe_stdin=True, rows=10, columns=40)
        self.addCleanup(process.close)
        self.assertTrue(process.wait_for(b"Resize terminal"), bytes(process.output))
        self.assert_terminal_success(process)


if __name__ == "__main__":
    unittest.main()
