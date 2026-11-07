import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "schwinn_bike"))
import protocol as p  # noqa: E402

PKT1 = bytes.fromhex("11 20 00 00 68 2A 00 00 43 FD 3E F5 00 00 00 00 02".replace(" ", ""))
PKT2 = bytes.fromhex("11 20 00 20 68 2A 00 00 50 FE 11 6D 01 00 00 00 02".replace(" ", ""))


class ParseTests(unittest.TestCase):
    def test_capture_vectors(self):
        a, b = p.parse_workout(PKT1), p.parse_workout(PKT2)
        self.assertEqual((a.cranks, a.ticks, a.resistance), (10856.0, 64835, 2))
        self.assertEqual((b.cranks, b.ticks), (10856.125, 65104))
        self.assertAlmostEqual(a.kcal, 0xF53E / 2097152)

    def test_ignored_lengths(self):
        self.assertIsNone(p.parse_workout(bytes.fromhex("0509010100")))
        self.assertIsNone(p.parse_workout(PKT1[:16]))
        self.assertIsNone(p.parse_workout(b"\x12" + PKT1[1:]))

    def test_motion_real_packets(self):
        # real packets from the 2026-11-06 ride: console showed 14.0 mph (level 4) and 17.4-17.7 mph (level 10) at these moments
        a = p.parse_motion(bytes.fromhex("170c61560560e8163d07003e2b00"))
        self.assertEqual((a.speed_mph, a.power_w, a.energy_count, a.distance_mi), (13.66, 58.64, 7, 0.43))
        b = p.parse_motion(bytes.fromhex("644461db066024 2b3d0b003e3b00".replace(" ", "")))
        self.assertEqual((b.speed_mph, b.power_w, b.energy_count, b.distance_mi), (17.55, 110.44, 11, 0.59))

    def test_motion_rejects_bad(self):
        self.assertIsNone(p.parse_motion(bytes(13)))
        self.assertIsNone(p.parse_motion(bytes.fromhex("170c62560560e8163d07003e2b00")))  # wrong tag
        self.assertIsNone(p.parse_motion(bytes.fromhex("170c61560560e8163d07003e2b0000")))  # wrong length

    def test_state_packet_carries_the_user(self):
        """live finding 2026-11-06: 05 09 <user> <state> 00; W = 1, Bob = 2; state 1 started, 3 ended"""
        self.assertEqual(p.parse_state(bytes.fromhex("0509010100")), (1, 1))  # W starts (seen in the first capture)
        self.assertEqual(p.parse_state(bytes.fromhex("0509020100")), (2, 1))  # Bob starts (seen in the Bob capture)
        self.assertEqual(p.parse_state(bytes.fromhex("0509010300")), (1, 3))  # W ends
        self.assertEqual(p.parse_state(bytes.fromhex("0509020300")), (2, 3))  # Bob ends: the old parser ignored this
        for bad in ("05090101", "0609010100", "0509010101", ""):
            self.assertIsNone(p.parse_state(bytes.fromhex(bad)))

    def test_user_from_hr_packet_byte7(self):
        d = bytearray(20); d[15] = 0x5A; d[7] = 2
        self.assertEqual(p.parse_user(bytes(d)), 2)  # also when there is no heart rate (b16 == 0)
        d[7] = 0
        self.assertIsNone(p.parse_user(bytes(d)))
        d[7] = 1; d[15] = 0
        self.assertIsNone(p.parse_user(bytes(d)))
        # a real packet from the Bob capture (heart rate 0: hands off) and one from the first Alice capture
        self.assertEqual(p.parse_user(bytes.fromhex("78200057001f2802190039013c00005a005c025f")), 2)
        self.assertEqual(p.parse_user(bytes.fromhex("01200189001f2801190039013c22005a005c045f")), 1)

    def test_user_map(self):
        self.assertEqual(p.parse_user_map("1=alice, 2=Bob"), {1: "alice", 2: "bob"})
        self.assertEqual(p.parse_user_map("1=a;2=b, x=c, 3=, =d"), {1: "a", 2: "b"})
        self.assertEqual(p.parse_user_map(""), {})

    def test_hr(self):
        d = bytearray(20); d[15] = 0x5A; d[16] = 132
        self.assertEqual(p.parse_hr(bytes(d)), 132)
        d[16] = 0
        self.assertIsNone(p.parse_hr(bytes(d)))
        d[16] = 120; d[15] = 0
        self.assertIsNone(p.parse_hr(bytes(d)))
        self.assertIsNone(p.parse_hr(bytes(19)))


if __name__ == "__main__":
    unittest.main()
