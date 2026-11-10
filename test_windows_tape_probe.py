import unittest

from windows_tape_probe import (ScsiPacket, checked_read_length, parse_capacity,
                                parse_mam, require_read_capacity)


def attr(aid, data):
    return aid.to_bytes(2, "big") + b"\x00" + len(data).to_bytes(2, "big") + data


class ProbeTests(unittest.TestCase):
    def test_read_rejects_check_condition_even_with_transferred_data(self):
        packet = ScsiPacket()
        packet.sptd.DataTransferLength = 524288
        packet.sptd.ScsiStatus = 2
        packet.sense[0] = 0xf0
        packet.sense[2] = 0x20  # ILI: record length differs from request
        with self.assertRaisesRegex(RuntimeError, "SCSI READ status"):
            checked_read_length(packet, 1048576)

    def test_read_rejects_invalid_transfer_lengths(self):
        packet = ScsiPacket()
        for length in (0, 1048577):
            packet.sptd.DataTransferLength = length
            with self.assertRaisesRegex(RuntimeError, "transfer length"):
                checked_read_length(packet, 1048576)
        packet.sptd.DataTransferLength = 1048576
        self.assertEqual(checked_read_length(packet, 1048576), 1048576)

    def test_adapter_limit_blocks_truncated_tape_reads(self):
        with self.assertRaisesRegex(RuntimeError, "splitting a tape record"):
            require_read_capacity({"max_transfer_bytes": 524288})
        require_read_capacity({"max_transfer_bytes": 1048576})

    def test_mam_identity_and_capacity(self):
        payload = (attr(0x400, b"SONY    ") + attr(0x401, b"L170321643") +
                   attr(0, (1100481).to_bytes(8, "big")))
        result = parse_mam(len(payload).to_bytes(4, "big") + payload)
        self.assertEqual(result["canonical_id"], "SONY:L170321643")
        self.assertEqual(result["remaining_mib"], 1100481)

    def test_mam_refuses_truncated_response(self):
        with self.assertRaises(ValueError):
            parse_mam(b"\0\0\0\x10\0")

    def test_capacity_parameters(self):
        def param(code, value):
            return code.to_bytes(2, "big") + b"\0\x04" + value.to_bytes(4, "big")
        payload = param(1, 1100481) + param(3, 2384185)
        result = parse_capacity(b"\x31\0" + len(payload).to_bytes(2, "big") + payload)
        self.assertEqual(result, {"remaining_mib": 1100481, "max_mib": 2384185})

    def test_capacity_refuses_truncation(self):
        with self.assertRaises(ValueError):
            parse_capacity(b"\x31\0\0\x06\0")


if __name__ == "__main__":
    unittest.main()
