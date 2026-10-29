"""CRSF protocol and channel conversion checks."""

import unittest

from crsf_ascii_visualizer import cli


def make_frame(channels=None, frame_type=0x16):
    """Build a packed frame independently from the stream parser."""
    channels = [992] * 16 if channels is None else channels
    bits = sum(value << (11 * index) for index, value in enumerate(channels))
    payload = bits.to_bytes(22, "little")
    body = bytes([frame_type]) + payload
    return bytes([0xC8, len(body) + 1]) + body + bytes([cli.crc8_dvb_s2(body)])


class ProtocolTests(unittest.TestCase):
    def test_crc8_dvb_s2_check_vector(self):
        self.assertEqual(cli.crc8_dvb_s2(b"123456789"), 0xBC)
        self.assertEqual(cli.crc8_dvb_s2(b""), 0)

    def test_unpack_sixteen_channels_across_byte_boundaries(self):
        channels = [0, 1, 172, 992, 1811, 2047, 1024, 511,
                    1536, 127, 1919, 819, 1640, 33, 1777, 7]
        frame = make_frame(channels)
        self.assertEqual(len(frame), 26)
        self.assertEqual(cli.unpack_channels(frame[3:-1]), channels)

    def test_fragmented_frame_waits_for_complete_payload_and_crc(self):
        frame = make_frame()
        parser = cli.CrsfFrameParser()
        for byte in frame[:-1]:
            parser.feed(bytes([byte]))
            self.assertEqual(list(parser.pop_frames()), [])
        parser.feed(frame[-1:])
        self.assertEqual(list(parser.pop_frames()), [(0x16, frame[3:-1])])
        self.assertEqual(list(parser.pop_frames()), [])

    def test_noise_and_invalid_length_resynchronize(self):
        frame = make_frame()
        parser = cli.CrsfFrameParser()
        parser.feed(b"\x00\xffnoise\xc8\x01\x00" + frame)
        self.assertEqual(list(parser.pop_frames()), [(0x16, frame[3:-1])])

    def test_bad_crc_is_dropped_without_losing_following_frame(self):
        bad = bytearray(make_frame([172] * 16))
        bad[-1] ^= 0x01
        good = make_frame([1811] * 16)
        parser = cli.CrsfFrameParser()
        parser.feed(bytes(bad) + good)
        frames = list(parser.pop_frames())
        self.assertEqual(frames, [(0x16, good[3:-1])])

    def test_multiple_frames_and_other_types_preserve_order(self):
        first = make_frame([172] * 16, frame_type=0x14)
        second = make_frame([1811] * 16)
        parser = cli.CrsfFrameParser()
        parser.feed(first + second)
        self.assertEqual(list(parser.pop_frames()), [
            (0x14, first[3:-1]), (0x16, second[3:-1]),
        ])

    def test_channel_conversion_endpoints_and_neutral(self):
        self.assertEqual([cli.crsf_to_us(n) for n in (172, 992, 1811)],
                         [988, 1500, 2012])
        self.assertEqual([cli.us_to_norm(n) for n in (988, 1500, 2012)],
                         [-1.0, 0.0, 1.0])
        self.assertEqual(cli.us_to_norm(0), -1.0)
        self.assertEqual(cli.us_to_norm(3000), 1.0)


if __name__ == "__main__":
    unittest.main()
