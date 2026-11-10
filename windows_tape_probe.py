#!/usr/bin/env python3
r"""Read-only Windows tape probe and experimental sample reader.

Run on Windows: python windows_tape_probe.py --device \\.\Tape0
The default probe never moves tape; --sample rewinds, positions, and reads.
This experimental diagnostic is not a tapebackup write or recovery backend.
"""
import argparse
import ctypes
import io
import json
import os
import struct
import sys
import tarfile


def parse_mam(data):
    if len(data) < 4:
        raise ValueError("short MAM response")
    end = 4 + int.from_bytes(data[:4], "big")
    if end > len(data):
        raise ValueError("truncated MAM response")
    attrs = {}
    i = 4
    while i < end:
        if i + 5 > end:
            raise ValueError("truncated MAM attribute header")
        aid = int.from_bytes(data[i:i + 2], "big")
        size = int.from_bytes(data[i + 3:i + 5], "big")
        if i + 5 + size > end:
            raise ValueError("truncated MAM attribute value")
        attrs[aid] = data[i + 5:i + 5 + size]
        i += 5 + size
    out = {}
    for aid, name in ((0x0000, "remaining_mib"), (0x0001, "max_mib"),
                      (0x0003, "load_count")):
        if aid in attrs:
            out[name] = int.from_bytes(attrs[aid], "big")
    for aid, name in ((0x0400, "manufacturer"), (0x0401, "medium_serial")):
        if aid in attrs:
            out[name] = attrs[aid].decode("ascii", "replace").strip("\x00 ")
    if out.get("manufacturer") and out.get("medium_serial"):
        out["canonical_id"] = out["manufacturer"] + ":" + out["medium_serial"]
    return out


def parse_capacity(data):
    if len(data) < 4 or data[0] & 0x3f != 0x31:
        raise ValueError("unexpected capacity log page")
    end = 4 + int.from_bytes(data[2:4], "big")
    if end > len(data):
        raise ValueError("truncated capacity log page")
    values = {}
    i = 4
    while i < end:
        if i + 4 > end or i + 4 + data[i + 3] > end:
            raise ValueError("truncated capacity log parameter")
        code = int.from_bytes(data[i:i + 2], "big")
        size = data[i + 3]
        values[code] = int.from_bytes(data[i + 4:i + 4 + size], "big")
        i += 4 + size
    return {name: values[code] for code, name in
            ((1, "remaining_mib"), (3, "max_mib")) if code in values}


class ScsiPassThroughDirect(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_uint16), ("ScsiStatus", ctypes.c_ubyte),
                ("PathId", ctypes.c_ubyte), ("TargetId", ctypes.c_ubyte),
                ("Lun", ctypes.c_ubyte), ("CdbLength", ctypes.c_ubyte),
                ("SenseInfoLength", ctypes.c_ubyte), ("DataIn", ctypes.c_ubyte),
                ("DataTransferLength", ctypes.c_uint32), ("TimeOutValue", ctypes.c_uint32),
                ("DataBuffer", ctypes.c_void_p), ("SenseInfoOffset", ctypes.c_uint32),
                ("Cdb", ctypes.c_ubyte * 16)]


class ScsiPacket(ctypes.Structure):
    _fields_ = [("sptd", ScsiPassThroughDirect), ("sense", ctypes.c_ubyte * 64)]


def adapter_limits(kernel, handle):
    """Query the adapter limit before moving tape or submitting a large read."""
    from ctypes import wintypes
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    query = ctypes.create_string_buffer(struct.pack("<II", 1, 0) + b"\0" * 8)
    buf = ctypes.create_string_buffer(1024)
    returned = wintypes.DWORD()
    if not kernel.DeviceIoControl(handle, 0x2d1400, query, len(query), buf, len(buf),
                                  ctypes.byref(returned), None):
        raise ctypes.WinError(ctypes.get_last_error())
    if returned.value < 20:
        raise RuntimeError("truncated storage adapter descriptor")
    _, _, maximum, pages, alignment = struct.unpack_from("<IIIII", buf.raw)
    return {"max_transfer_bytes": maximum, "max_physical_pages": pages,
            "alignment_mask": alignment}


def require_read_capacity(limits, block_size=1024 * 1024):
    if limits["max_transfer_bytes"] < block_size:
        raise RuntimeError("adapter maximum transfer is %d bytes; archived tape blocks require "
                           "at least %d bytes. Fix the Windows HBA driver limit first; "
                           "splitting a tape record into smaller reads is not safe." %
                           (limits["max_transfer_bytes"], block_size))


def checked_read_length(packet, capacity):
    # Fail closed on check conditions, including ILI/truncated records and
    # filemarks. This sample reader must find its member before a filemark.
    if packet.sptd.ScsiStatus:
        raise RuntimeError("SCSI READ status %#x, sense %s" %
                           (packet.sptd.ScsiStatus, bytes(packet.sense).hex()))
    length = packet.sptd.DataTransferLength
    if not 0 < length <= capacity:
        raise RuntimeError("invalid SCSI READ transfer length: %d" % length)
    return length


def probe(device, _handle=None):
    if os.name != "nt":
        raise RuntimeError("this probe must run on Windows")
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = (_handle if _handle is not None else
              kernel.CreateFileW(device, 0xC0000000, 3, None, 3, 0, None))
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    def read_scsi(cdb, alloc):
        # Restrict this program to the three explicitly read-only commands.
        if cdb[0] not in (0x12, 0x4d, 0x8c):
            raise ValueError("SCSI command is not allowed")
        buf = ctypes.create_string_buffer(alloc)
        pkt = ScsiPacket()
        pkt.sptd.Length = ctypes.sizeof(ScsiPassThroughDirect)
        pkt.sptd.CdbLength = len(cdb)
        pkt.sptd.SenseInfoLength = len(pkt.sense)
        pkt.sptd.DataIn = 1  # SCSI_IOCTL_DATA_IN
        pkt.sptd.DataTransferLength = alloc
        pkt.sptd.TimeOutValue = 60
        pkt.sptd.DataBuffer = ctypes.addressof(buf)
        pkt.sptd.SenseInfoOffset = ScsiPacket.sense.offset
        pkt.sptd.Cdb[:len(cdb)] = cdb
        returned = wintypes.DWORD()
        if not kernel.DeviceIoControl(handle, 0x4D014, ctypes.byref(pkt), ctypes.sizeof(pkt),
                                      ctypes.byref(pkt), ctypes.sizeof(pkt), ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if pkt.sptd.ScsiStatus:
            raise RuntimeError("SCSI status %#x, sense %s" %
                               (pkt.sptd.ScsiStatus, bytes(pkt.sense).hex()))
        return buf.raw[:min(alloc, pkt.sptd.DataTransferLength)]

    try:
        inquiry = read_scsi(bytes([0x12, 0, 0, 0, 96, 0]), 96)
        if len(inquiry) < 36 or inquiry[0] & 0x1f != 1:
            raise RuntimeError("device is not a SCSI sequential-access tape drive")
        result = {"device": device, "vendor": inquiry[8:16].decode("ascii", "replace").strip(),
                  "model": inquiry[16:32].decode("ascii", "replace").strip(),
                  "revision": inquiry[32:36].decode("ascii", "replace").strip()}
        cdb = bytearray(16)
        cdb[0] = 0x8c  # READ ATTRIBUTE, service action 0
        cdb[10:14] = (8192).to_bytes(4, "big")
        cdb[14] = 1  # cached MAM, matching the Linux implementation
        result["mam"] = parse_mam(read_scsi(cdb, 8192))
        result["adapter"] = adapter_limits(kernel, handle)
        cdb = bytearray(10)
        cdb[0] = 0x4d  # LOG SENSE, current cumulative values
        cdb[2] = 0x40 | 0x31
        cdb[7:9] = (512).to_bytes(2, "big")
        result["capacity_log"] = parse_capacity(read_scsi(cdb, 512))
        return result
    finally:
        if _handle is None:
            kernel.CloseHandle(handle)


def read_sample(device, expected_id, file_number, member, output):
    """Position read-only and stream one tar member; never issue tape writes."""
    if file_number < 1 or not member or member.startswith("/") or ".." in member.split("/"):
        raise ValueError("invalid tape file number or member name")
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.SetTapePosition.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    kernel.SetTapePosition.restype = wintypes.DWORD
    kernel.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    kernel.VirtualAlloc.restype = ctypes.c_void_p
    kernel.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]
    kernel.VirtualFree.restype = wintypes.BOOL
    # Exclusive access prevents a second Windows process moving the tape.
    handle = kernel.CreateFileW(device, 0xC0000000, 0, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        actual_id = probe(device, _handle=handle).get("mam", {}).get("canonical_id")
        if actual_id != expected_id:
            raise RuntimeError("wrong cartridge: expected %s, found %s" % (expected_id, actual_id))
        require_read_capacity(adapter_limits(kernel, handle))
    except Exception:
        kernel.CloseHandle(handle)
        raise
    block_buffer_size = 1024 * 1024
    read_buffer = kernel.VirtualAlloc(None, block_buffer_size, 0x3000, 0x04)
    if not read_buffer:
        kernel.CloseHandle(handle)
        raise ctypes.WinError(ctypes.get_last_error())

    class TapeReader(io.RawIOBase):
        def __init__(self):
            super().__init__()
            self.pending = b""

        def readable(self):
            return True

        def readinto(self, dest):
            if not len(dest):
                return 0
            if not self.pending:
                pkt = ScsiPacket()
                pkt.sptd.Length = ctypes.sizeof(ScsiPassThroughDirect)
                pkt.sptd.CdbLength = 6
                pkt.sptd.SenseInfoLength = len(pkt.sense)
                pkt.sptd.DataIn = 1
                pkt.sptd.DataTransferLength = block_buffer_size
                pkt.sptd.TimeOutValue = 120
                pkt.sptd.DataBuffer = read_buffer
                pkt.sptd.SenseInfoOffset = ScsiPacket.sense.offset
                # READ(6), variable-length record, SILI=0: never hide truncation.
                pkt.sptd.Cdb[:6] = bytes([0x08, 0]) + block_buffer_size.to_bytes(3, "big") + b"\0"
                returned = wintypes.DWORD()
                if not kernel.DeviceIoControl(handle, 0x4D014, ctypes.byref(pkt), ctypes.sizeof(pkt),
                                              ctypes.byref(pkt), ctypes.sizeof(pkt),
                                              ctypes.byref(returned), None):
                    raise ctypes.WinError(ctypes.get_last_error())
                length = checked_read_length(pkt, block_buffer_size)
                self.pending = ctypes.string_at(read_buffer, length)
            count = min(len(dest), len(self.pending))
            dest[:count] = self.pending[:count]
            self.pending = self.pending[count:]
            return count

    try:
        for method, count in ((0, 0), (6, file_number - 1)):  # rewind; space filemarks
            if method == 6 and not count:
                continue
            status = kernel.SetTapePosition(handle, method, 0, count, 0, False)
            if status:
                raise RuntimeError("SetTapePosition method %d count %d: %s" %
                                   (method, count, ctypes.WinError(status)))
        with io.BufferedReader(TapeReader(), buffer_size=block_buffer_size) as stream:
            with tarfile.open(fileobj=stream, mode="r|") as archive:
                for item in archive:
                    if item.name == member and item.isfile():
                        source = archive.extractfile(item)
                        if source is None:
                            raise RuntimeError("cannot read requested archive member")
                        while True:
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            output.write(block)
                        return
        raise RuntimeError("member not found in specified tape file")
    finally:
        kernel.VirtualFree(read_buffer, 0, 0x8000)
        kernel.CloseHandle(handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=r"\\.\Tape0")
    parser.add_argument("--sample", help="exact root-relative tar member to read to stdout")
    parser.add_argument("--file-number", type=int, help="1-based tape file number")
    parser.add_argument("--expected-id", help="required cartridge identity for sample read")
    args = parser.parse_args()
    try:
        if args.sample:
            if not args.file_number or not args.expected_id:
                parser.error("--sample requires --file-number and --expected-id")
            read_sample(args.device, args.expected_id, args.file_number, args.sample, sys.stdout.buffer)
        else:
            print(json.dumps(probe(args.device), ensure_ascii=True))
    except Exception as e:
        print(json.dumps({"error": str(e), "device": args.device}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
