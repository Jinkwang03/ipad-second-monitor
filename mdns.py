"""A tiny mDNS responder so the iPad can reach the PC at a fixed name like ipad-display.local.

Safari resolves ".local" names by multicasting a DNS question on the local network. We
answer with the PC's address on the network the question came from (Wi-Fi, the laptop's
mobile hotspot, ...), so the same http://<name>.local link keeps working when the PC's IP
address changes. Standard library only.
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time

log = logging.getLogger("ipad-display")

GROUP, PORT = "224.0.0.251", 5353
TYPE_A, TYPE_ANY = 1, 255
CLASS_IN, CACHE_FLUSH, UNICAST_RESPONSE = 1, 0x8000, 0x8000
FLAG_RESPONSE = 0x8000
TTL, LEGACY_TTL = 60, 10          # seconds; RFC 6762 caps legacy unicast answers at 10
REFRESH = 5.0                     # seconds between checks for new network interfaces
HEADER = struct.Struct(">HHHHHH")


def encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def decode_name(buf: bytes, off: int) -> tuple[str, int]:
    """Read a (possibly compressed) DNS name; returns (name, offset after it)."""
    labels, end, jumps = [], None, 0
    while True:
        length = buf[off]
        if length & 0xC0 == 0xC0:                       # compression pointer
            if jumps > 20:
                raise ValueError("pointer loop")
            if end is None:
                end = off + 2
            off = ((length & 0x3F) << 8) | buf[off + 1]
            jumps += 1
            continue
        off += 1
        if length == 0:
            break
        labels.append(buf[off:off + length].decode("utf-8", "replace"))
        off += length
    return ".".join(labels), (end if end is not None else off)


def parse_query(packet: bytes):
    """Return (id, [(name, qtype, qclass), ...]) for a query, or None for responses/garbage."""
    try:
        qid, flags, qdcount, _, _, _ = HEADER.unpack_from(packet, 0)
        if flags & FLAG_RESPONSE:
            return None
        off, questions = HEADER.size, []
        for _ in range(qdcount):
            name, off = decode_name(packet, off)
            qtype, qclass = struct.unpack_from(">HH", packet, off)
            off += 4
            questions.append((name, qtype, qclass))
        return qid, questions
    except (struct.error, IndexError, ValueError):
        return None


def build_answer(name: str, ip: str, query_id: int = 0, question=None,
                 ttl: int = TTL, cache_flush: bool = True) -> bytes:
    """An authoritative response carrying one A record (plus the question, for legacy queries)."""
    header = HEADER.pack(query_id, FLAG_RESPONSE | 0x0400, 1 if question else 0, 1, 0, 0)
    body = b""
    if question:
        qname, qtype = question
        body += encode_name(qname) + struct.pack(">HH", qtype, CLASS_IN)
    rclass = CLASS_IN | (CACHE_FLUSH if cache_flush else 0)
    body += encode_name(name) + struct.pack(">HHIH", TYPE_A, rclass, ttl, 4) + socket.inet_aton(ip)
    return header + body


def route_ip(peer: str) -> str | None:
    """The PC's own address on the network that reaches `peer` (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((peer, PORT))
            return s.getsockname()[0]
    except OSError:
        return None


def local_ipv4() -> set[str]:
    found = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    default = route_ip("10.254.254.254")
    if default:
        found.add(default)
    return {ip for ip in found if not ip.startswith(("127.", "169.254.", "0."))}


class MdnsResponder:
    """Answers A queries for <name>.local on every network interface, in a background thread."""

    def __init__(self, name: str):
        self.host = f"{name}.local"
        self.sock: socket.socket | None = None
        self.joined: set[str] = set()
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # share 5353 with other responders
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", PORT))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.settimeout(1.0)
        self.sock = sock
        self._join_groups()
        self.thread = threading.Thread(target=self._run, name="mdns", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        if self.sock is not None:
            self.sock.close()

    # ---- internals (responder thread) ------------------------------------

    def _join_groups(self) -> bool:
        """Join the mDNS group on any new interface; True if the set of interfaces changed."""
        current = local_ipv4()
        for ip in current - self.joined:
            try:
                mreq = socket.inet_aton(GROUP) + socket.inet_aton(ip)
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError:
                pass   # already a member, or the interface just went away
        changed = current != self.joined
        self.joined = current
        return changed

    def _send_multicast(self, packet: bytes, iface_ip: str) -> None:
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip))
            self.sock.sendto(packet, (GROUP, PORT))
        except OSError as exc:
            log.debug("mDNS send on %s failed: %s", iface_ip, exc)

    def _announce(self) -> None:
        """Tell each network our (possibly new) address so stale caches update right away."""
        for ip in self.joined:
            self._send_multicast(build_answer(self.host, ip), ip)

    def _run(self) -> None:
        self._announce()
        next_refresh = time.monotonic() + REFRESH
        while not self.stopping.is_set():
            if time.monotonic() >= next_refresh:
                next_refresh = time.monotonic() + REFRESH
                if self._join_groups():
                    self._announce()
            try:
                packet, (src, sport) = self.sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:          # e.g. Windows reports ICMP "port unreachable" as a reset
                if self.stopping.is_set():
                    return
                continue
            self._handle(packet, src, sport)

    def _handle(self, packet: bytes, src: str, sport: int) -> None:
        query = parse_query(packet)
        if query is None:
            return
        qid, questions = query
        for name, qtype, qclass in questions:
            if name.lower() != self.host.lower() or qtype not in (TYPE_A, TYPE_ANY):
                continue
            ip = route_ip(src)
            if ip is None:
                return
            try:
                if sport != PORT:     # "legacy" one-shot resolver: reply straight to it
                    self.sock.sendto(build_answer(self.host, ip, qid, (name, qtype), LEGACY_TTL, False),
                                     (src, sport))
                elif qclass & UNICAST_RESPONSE:
                    self.sock.sendto(build_answer(self.host, ip), (src, PORT))
                else:
                    self._send_multicast(build_answer(self.host, ip), ip)
            except OSError as exc:
                log.debug("mDNS reply to %s failed: %s", src, exc)
            return
