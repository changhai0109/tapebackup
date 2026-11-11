#!/usr/bin/env python3
"""Experimental Windows append worker: staged archives, filemarks and full readback."""
import argparse
import ctypes
import datetime
import hashlib
import json
import os
import re

from windows_tape_probe import (ScsiPacket, ScsiPassThroughDirect, adapter_limits,
                                checked_read_length, probe, require_read_capacity)

BLOCK = 1024 * 1024


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_manifest(config):
    for name in ("sid", "cartridge"):
        if not isinstance(config.get(name), str) or not config[name]:
            raise ValueError("missing %s" % name)
    for name, minimum in (("data_file", 2), ("start_logical_object", 1)):
        if type(config.get(name)) is not int or not minimum <= config[name] < 2**63:
            raise ValueError("invalid %s" % name)
    archives = config.get("archives", [])
    if len(archives) != 2 or [a.get("name") for a in archives] != ["chunk.tar", "index.tar"]:
        raise ValueError("expected chunk.tar followed by index.tar")
    for item in archives:
        size = item.get("size")
        if type(size) is not int or size <= 0 or size % BLOCK:
            raise ValueError("archive size must be a positive multiple of 1 MiB")
        if not re.fullmatch(r"[0-9a-f]{64}", item.get("sha256", "")):
            raise ValueError("invalid archive SHA-256")


def parse_position(data):
    if len(data) != 32 or data[0] & 0x0c:
        raise RuntimeError("short or unknown tape position")
    return [int.from_bytes(data[4:8], "big"),
            int.from_bytes(data[8:16], "big"), int.from_bytes(data[16:24], "big")]


def is_filemark(packet):
    sense = bytes(packet.sense)
    return (packet.sptd.ScsiStatus == 2 and sense[0] == 0xf0 and
            sense[2] == 0x80 and sense[12:14] == b"\0\1" and
            int.from_bytes(sense[3:7], "big") == BLOCK)


class WindowsTape:
    """Hold one exclusive handle from identity check through final verification."""
    def __init__(self, device):
        if os.name != "nt":
            raise RuntimeError("the tape worker must run on Windows")
        from ctypes import wintypes as w
        self.w = w
        self.k = k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                 w.DWORD, w.DWORD, w.HANDLE]
        k.CreateFileW.restype = w.HANDLE
        k.CloseHandle.argtypes = [w.HANDLE]
        k.SetTapePosition.argtypes = [w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.BOOL]
        k.SetTapePosition.restype = w.DWORD
        k.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, w.DWORD, w.DWORD]
        k.VirtualAlloc.restype = ctypes.c_void_p
        k.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, w.DWORD]
        self.device = device
        self.buf = None
        self.handle = k.CreateFileW(device, 0xc0000000, 0, None, 3, 0, None)
        if self.handle == ctypes.c_void_p(-1).value:
            self.handle = None
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            require_read_capacity(adapter_limits(k, self.handle))
            self.buf = k.VirtualAlloc(None, BLOCK, 0x3000, 4)
            if not self.buf:
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.buf:
            self.k.VirtualFree(self.buf, 0, 0x8000)
            self.buf = None
        if self.handle is not None:
            self.k.CloseHandle(self.handle)
            self.handle = None

    def _command(self, cdb, length=0, direction=2):
        if cdb[0] not in (0x00, 0x08, 0x0a, 0x10, 0x34):
            raise ValueError("disallowed opcode")
        packet = ScsiPacket()
        packet.sptd.Length = ctypes.sizeof(ScsiPassThroughDirect)
        packet.sptd.CdbLength = len(cdb)
        packet.sptd.Cdb[:len(cdb)] = cdb
        packet.sptd.SenseInfoLength = 64
        packet.sptd.SenseInfoOffset = ScsiPacket.sense.offset
        packet.sptd.DataIn = direction
        packet.sptd.DataTransferLength = length
        packet.sptd.DataBuffer = self.buf if length else None
        packet.sptd.TimeOutValue = 180
        returned = self.w.DWORD()
        if not self.k.DeviceIoControl(self.handle, 0x4d014, ctypes.byref(packet),
                                     ctypes.sizeof(packet), ctypes.byref(packet),
                                     ctypes.sizeof(packet), ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return packet

    @staticmethod
    def _good(packet):
        if packet.sptd.ScsiStatus:
            raise RuntimeError("SCSI status %d, sense %s" %
                               (packet.sptd.ScsiStatus, bytes(packet.sense).hex()))

    def identity(self):
        return probe(self.device, _handle=self.handle)["mam"]["canonical_id"]

    def ready(self):
        self._good(self._command(bytes(6)))

    def seek(self, method, count=0):
        status = self.k.SetTapePosition(self.handle, method, 0, count & 0xffffffff,
                                       count >> 32, False)
        if status:
            raise ctypes.WinError(status)

    def position(self):
        packet = self._command(bytes([0x34, 6, 0, 0, 0, 0, 0, 0, 0, 0]), 32, 1)
        self._good(packet)
        if packet.sptd.DataTransferLength != 32:
            raise RuntimeError("short READ POSITION")
        return parse_position(ctypes.string_at(self.buf, 32))

    def write(self, data):
        if len(data) != BLOCK:
            raise ValueError("writes must be one 1 MiB record")
        ctypes.memmove(self.buf, data, BLOCK)
        packet = self._command(bytes([0x0a, 0, 16, 0, 0, 0]), BLOCK, 0)
        self._good(packet)
        if packet.sptd.DataTransferLength != BLOCK:
            raise RuntimeError("short write; do not retry automatically")

    def write_filemark(self):
        # IMMED=0: completion flushes the buffered data and one filemark.
        self._good(self._command(bytes([0x10, 0, 0, 0, 1, 0])))

    def read(self):
        packet = self._command(bytes([8, 0, 16, 0, 0, 0]), BLOCK, 1)
        if checked_read_length(packet, BLOCK) != BLOCK:
            raise RuntimeError("short readback record")
        return ctypes.string_at(self.buf, BLOCK)

    def expect_filemark(self):
        packet = self._command(bytes([8, 0, 16, 0, 0, 0]), BLOCK, 1)
        if not is_filemark(packet):
            raise RuntimeError("expected filemark, sense %s" % bytes(packet.sense).hex())


def append_verified(root, config, tape, log):
    """No truncation/recovery: EOD must equal the caller's verified boundary."""
    validate_manifest(config)
    if tape.identity() != config["cartridge"]:
        raise RuntimeError("wrong cartridge")
    tape.ready()
    tape.seek(4)  # SPACE END OF DATA
    expected = [0, config["start_logical_object"], config["data_file"] - 1]
    if tape.position() != expected:
        raise RuntimeError("EOD differs from the expected append boundary")
    log("append_boundary_confirmed", position=expected)
    for item in config["archives"]:
        if tape.position() != expected:
            raise RuntimeError("append position changed")
        start = expected[:]
        log("write_intent", archive=item["name"], position=start, size=item["size"])
        count = 0
        with open(os.path.join(root, item["name"]), "rb") as source:
            for data in iter(lambda: source.read(BLOCK), b""):
                if count * BLOCK >= item["size"]:
                    raise RuntimeError("staged archive grew")
                tape.write(data)
                count += 1
                if count % 1024 == 0:
                    tape.ready()
                    log("write_progress", archive=item["name"], bytes=count * BLOCK)
        if count * BLOCK != item["size"]:
            raise RuntimeError("staged archive changed")
        tape.write_filemark()
        expected = [0, start[1] + count + 1, start[2] + 1]
        if tape.position() != expected:
            raise RuntimeError("post-filemark position mismatch")
        log("filemark_committed", archive=item["name"], position=expected)
        tape.seek(2, start[1])  # LOGICAL BLOCK
        if tape.position() != start:
            raise RuntimeError("readback locate mismatch")
        digest = hashlib.sha256()
        for i in range(count):
            digest.update(tape.read())
            if (i + 1) % 1024 == 0:
                tape.ready()
                log("readback_progress", archive=item["name"], bytes=(i + 1) * BLOCK)
        if digest.hexdigest() != item["sha256"]:
            raise RuntimeError("readback SHA-256 mismatch")
        tape.expect_filemark()
        tape.seek(4)
        if tape.position() != expected:
            raise RuntimeError("EOD mismatch after readback")
        log("archive_verified", archive=item["name"], sha256=digest.hexdigest(), eod=expected)
    log("success", sid=config["sid"], eod=expected)
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", help="staging directory containing manifest.json and tar files")
    parser.add_argument("--device", default=r"\\.\Tape0")
    args = parser.parse_args()
    root = os.path.abspath(args.directory)
    with open(os.path.join(root, "manifest.json")) as source:
        config = json.load(source)
    validate_manifest(config)

    def log(stage, **fields):
        fields.update(stage=stage, time=datetime.datetime.now().isoformat())
        with open(os.path.join(root, "journal.jsonl"), "a") as journal:
            journal.write(json.dumps(fields) + "\n")
            journal.flush()
            os.fsync(journal.fileno())
        print(json.dumps(fields), flush=True)

    # Durable one-shot marker also covers failures before the first write.
    with open(os.path.join(root, "started"), "x") as marker:
        marker.write(config["sid"])
        marker.flush()
        os.fsync(marker.fileno())
    tape = None
    try:
        for item in config["archives"]:
            path = os.path.join(root, item["name"])
            if os.path.getsize(path) != item["size"] or digest_file(path) != item["sha256"]:
                raise RuntimeError("staged archive verification failed: " + item["name"])
        log("staged_archives_verified")
        tape = WindowsTape(args.device)
        append_verified(root, config, tape, log)
    except BaseException as error:
        log("error", error=str(error), action="inspect journal; no automatic write retry")
        raise
    finally:
        if tape is not None:
            tape.close()


if __name__ == "__main__":
    main()
