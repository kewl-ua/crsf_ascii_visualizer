"""Exercise actual loopback sockets and source cleanup."""

import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

from crsf_ascii_visualizer import cli


class UdpSourceTests(unittest.TestCase):
    def test_drains_all_queued_datagrams_in_order(self):
        source = cli.UdpSource("127.0.0.1", 0)
        self.addCleanup(source.close)
        address = source.sock.getsockname()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for packet in (b"first", b"second", b"third"):
                sender.sendto(packet, address)
            self.assertEqual(source.read(timeout=0.5), b"firstsecondthird")
        self.assertEqual(source.read(timeout=0.01), b"")


class StdinSourceTests(unittest.TestCase):
    def test_close_preserves_the_callers_original_pipe_descriptor(self):
        reader, writer = os.pipe()
        try:
            source = cli.StdinSource(reader)
            source.close()
            source.close()
            os.write(writer, b"original descriptor")
            self.assertEqual(os.read(reader, 64), b"original descriptor")
        finally:
            os.close(reader)
            os.close(writer)


class UnixSourceConstructionTests(unittest.TestCase):
    def test_post_bind_stat_failure_closes_the_new_socket(self):
        fake_socket = mock.Mock()
        with mock.patch.object(cli.os.path, "lexists", return_value=False), \
                mock.patch.object(cli.socket, "AF_UNIX", object(), create=True), \
                mock.patch.object(cli.socket, "socket", return_value=fake_socket), \
                mock.patch.object(cli.os, "stat", side_effect=FileNotFoundError("socket removed after bind")):
            with self.assertRaises(FileNotFoundError):
                cli.UnixDgramSource("/tmp/crsf.sock")
        fake_socket.bind.assert_called_once_with("/tmp/crsf.sock")
        fake_socket.close.assert_called_once_with()


@unittest.skipUnless(os.name == "posix" and hasattr(socket, "AF_UNIX"),
                     "UNIX sockets require a POSIX host")
class UnixSourceTests(unittest.TestCase):
    def test_datagram_source_does_not_delete_an_existing_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keep.txt"
            path.write_bytes(b"unrelated data")
            with self.assertRaises(OSError):
                cli.UnixDgramSource(str(path))
            self.assertEqual(path.read_bytes(), b"unrelated data")

    def test_datagram_source_does_not_clobber_an_existing_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "crsf.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as listener:
                listener.bind(path)
                with self.assertRaises(OSError):
                    cli.UnixDgramSource(path)
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
                    sender.sendto(b"still bound", path)
                listener.settimeout(0.5)
                self.assertEqual(listener.recv(64), b"still bound")

    def test_datagram_source_drains_and_removes_owned_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "crsf.sock")
            source = cli.UnixDgramSource(path)
            try:
                self.assertTrue(Path(path).exists())
                with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
                    sender.sendto(b"one", path)
                    sender.sendto(b"two", path)
                self.assertEqual(source.read(timeout=0.5), b"onetwo")
                self.assertEqual(source.read(timeout=0.01), b"")
            finally:
                source.close()
            self.assertFalse(Path(path).exists())

    def test_datagram_close_preserves_a_replacement_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crsf.sock"
            source = cli.UnixDgramSource(str(path))
            try:
                path.unlink()
                path.write_bytes(b"replacement data")
            finally:
                source.close()
            self.assertEqual(path.read_bytes(), b"replacement data")

    def test_stream_source_connects_and_reports_peer_eof(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "crsf.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(path)
                server.listen(1)
                source = cli.UnixStreamSource(path)
                try:
                    connection, _ = server.accept()
                    with connection:
                        connection.sendall(b"packed CRSF bytes")
                        self.assertEqual(source.read(timeout=0.5),
                                         b"packed CRSF bytes")
                    with self.assertRaises(EOFError):
                        source.read(timeout=0.5)
                finally:
                    source.close()


if __name__ == "__main__":
    unittest.main()
