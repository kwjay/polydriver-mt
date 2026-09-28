import unittest
import struct
from comms.constants import *
from comms.link_layer import calculate_crc8, build_frame, ProtocolParser, MAX_PAYLOAD_LEN


class TestLinkLayer(unittest.TestCase):
    def setUp(self):
        self.parser = ProtocolParser()

    def test_crc8_consistency(self):
        crc_data = bytes([0x01, CMD_REQ_STAT, 0x00])
        crc = calculate_crc8(crc_data)

        self.assertEqual(crc, calculate_crc8(crc_data))
        crc_data_altered = bytes([0x02, CMD_REQ_STAT, 0x00])
        self.assertNotEqual(crc, calculate_crc8(crc_data_altered))

    def test_build_frame_structure(self):
        target_id = 0x02
        cmd = CMD_SET_SPEED
        payload = struct.pack('<f', 15.5)

        frame = build_frame(target_id, cmd, payload)

        self.assertEqual(frame[0], STX)
        self.assertEqual(frame[1], target_id)
        self.assertEqual(frame[2], cmd)
        self.assertEqual(frame[3], len(payload))
        self.assertEqual(frame[4:8], payload)
        self.assertEqual(frame[-1], ETX)

    def test_build_frame_rejects_oversized_payload(self):
        oversized_payload = bytes(MAX_PAYLOAD_LEN + 1)
        with self.assertRaises(ValueError):
            build_frame(0x01, CMD_SET_SPEED, oversized_payload)

    def test_parser_valid_frame(self):
        target_id = 0x01
        cmd = RESP_STATUS
        payload = struct.pack('<f', 50.0) + bytes([128, 0])

        frame = build_frame(target_id, cmd, payload)

        parsed_result = None

        for byte in frame:
            parsed_result = self.parser.process_byte(byte)

        self.assertIsNotNone(parsed_result)
        if parsed_result is not None:
            self.assertEqual(parsed_result.source_id, target_id)
            self.assertEqual(parsed_result.command, cmd)
            self.assertEqual(parsed_result.length, 6)
            self.assertEqual(parsed_result.payload, payload)

    def test_parser_accepts_a_full_size_status_response(self):
        payload = bytes(range(MAX_RESPONSE_PAYLOAD_LEN))
        body = bytes([0x01, RESP_STATUS, len(payload)]) + payload
        frame = bytes([STX]) + body + bytes([calculate_crc8(body), ETX])
        parsed_result = None
        for byte in frame:
            parsed_result = self.parser.process_byte(byte)
        self.assertIsNotNone(parsed_result)
        self.assertEqual(parsed_result.payload, payload)

    def test_parser_rejects_a_length_above_the_response_limit(self):
        parsed_result = None
        for byte in bytes([STX, 0x01, RESP_STATUS, MAX_RESPONSE_PAYLOAD_LEN + 1]):
            parsed_result = self.parser.process_byte(byte)
        self.assertIsNone(parsed_result)
        self.assertEqual(self.parser.state, RxState.WAIT_STX)

    def test_parser_crc_rejection(self):
        target_id = 0x01
        cmd = RESP_ACK
        frame = bytearray(build_frame(target_id, cmd, b''))

        frame[2] = RESP_NACK

        parsed_result = None
        for byte in frame:
            parsed_result = self.parser.process_byte(byte)

        self.assertIsNone(parsed_result)

    def test_parser_noise_recovery(self):
        noise = bytes([0xFF, 0x55, 0x00, ETX])
        valid_frame = build_frame(0x01, CMD_REQ_STAT, b'')

        stream = noise + valid_frame

        parsed_result = None
        for byte in stream:
            result = self.parser.process_byte(byte)
            if result is not None:
                parsed_result = result

        self.assertIsNotNone(parsed_result)
        if parsed_result is not None:
            self.assertEqual(parsed_result.command, CMD_REQ_STAT)

    def test_parser_recovers_from_corrupt_length_byte(self):
        malformed = bytes([STX, 0x01, CMD_REQ_STAT, 200])
        for byte in malformed:
            result = self.parser.process_byte(byte)
            self.assertIsNone(result)

        valid_frame = build_frame(0x01, CMD_REQ_STAT, b'')
        parsed_result = None
        for byte in valid_frame:
            result = self.parser.process_byte(byte)
            if result is not None:
                parsed_result = result
        self.assertIsNotNone(parsed_result)
        if parsed_result is not None:
            self.assertEqual(parsed_result.command, CMD_REQ_STAT)


if __name__ == '__main__':
    unittest.main()