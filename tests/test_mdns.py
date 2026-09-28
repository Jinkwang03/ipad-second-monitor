"""mDNS responder tests: packet encoding/decoding, and real queries over the network stack."""
import os
import secrets
import socket
import struct
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mdns  # noqa: E402


def query(name, qid=0x4242, qclass=mdns.CLASS_IN, qtype=mdns.TYPE_A):
    return (struct.pack(">HHHHHH", qid, 0, 1, 0, 0, 0) + mdns.encode_name(name)
            + struct.pack(">HH", qtype, qclass))


def read_answer(packet):
    """(query id, answer name, answer IPv4, ttl, rclass) from a one-answer response."""
    qid, flags, qdcount, ancount, _, _ = struct.unpack_from(">HHHHHH", packet)
    assert flags & mdns.FLAG_RESPONSE and ancount == 1
    off = 12
    for _ in range(qdcount):
        _, off = mdns.decode_name(packet, off)
        off += 4
    name, off = mdns.decode_name(packet, off)
    rtype, rclass, ttl, rdlen = struct.unpack_from(">HHIH", packet, off)
    assert rtype == mdns.TYPE_A and rdlen == 4
    return qid, name, socket.inet_ntoa(packet[off + 10:off + 14]), ttl, rclass


class PacketTests(unittest.TestCase):
    def test_name_roundtrip(self):
        encoded = mdns.encode_name("ipad-display.local")
        self.assertEqual(encoded, b"\x0cipad-display\x05local\x00")
        self.assertEqual(mdns.decode_name(encoded, 0), ("ipad-display.local", len(encoded)))

    def test_compressed_name(self):
        # "local" at offset 0, then "ipad-display" + pointer back to offset 0
        buf = b"\x05local\x00" + b"\x0cipad-display\xc0\x00"
        self.assertEqual(mdns.decode_name(buf, 7), ("ipad-display.local", len(buf)))

    def test_pointer_loop_is_rejected(self):
        self.assertIsNone(mdns.parse_query(struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + b"\xc0\x0c"))

    def test_parse_query_and_ignore_responses(self):
        qid, questions = mdns.parse_query(query("Ipad-Display.local", qid=7, qclass=0x8001))
        self.assertEqual((qid, questions), (7, [("Ipad-Display.local", 1, 0x8001)]))
        self.assertIsNone(mdns.parse_query(mdns.build_answer("x.local", "10.0.0.1")))
        self.assertIsNone(mdns.parse_query(b"\x00\x01"))

    def test_multicast_answer(self):
        qid, name, ip, ttl, rclass = read_answer(mdns.build_answer("ipad-display.local", "192.168.0.23"))
        self.assertEqual((qid, name, ip, ttl, rclass), (0, "ipad-display.local", "192.168.0.23", 60, 0x8001))

    def test_legacy_answer_echoes_question(self):
        packet = mdns.build_answer("a.local", "10.1.2.3", 99, ("a.local", 1), mdns.LEGACY_TTL, False)
        self.assertEqual(struct.unpack_from(">H", packet, 4)[0], 1)          # question included
        self.assertEqual(read_answer(packet), (99, "a.local", "10.1.2.3", 10, 1))


@unittest.skipUnless(sys.platform == "win32" and not os.environ.get("CI"),
                     "network test runs on a real Windows PC (CI runners restrict multicast)")
class LiveTests(unittest.TestCase):
    """Start a responder under a random name and query it the way real clients do."""

    @classmethod
    def setUpClass(cls):
        cls.name = f"ipd-test-{secrets.token_hex(3)}"
        cls.responder = mdns.MdnsResponder(cls.name)
        cls.responder.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.responder.stop()

    def test_one_shot_query_gets_direct_answer(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(3)
            s.sendto(query(f"{self.name}.local", qid=0x1234), (mdns.GROUP, mdns.PORT))
            packet, (src, _) = s.recvfrom(2048)
        qid, name, ip, ttl, _ = read_answer(packet)
        self.assertEqual((qid, name, ttl), (0x1234, f"{self.name}.local", mdns.LEGACY_TTL))
        self.assertEqual(ip, mdns.route_ip(src))       # the address on the network that asked

    def test_other_names_are_ignored(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(1.5)
            s.sendto(query(f"not-{self.name}.local"), (mdns.GROUP, mdns.PORT))
            with self.assertRaises(socket.timeout):
                s.recvfrom(2048)


if __name__ == "__main__":
    unittest.main()
