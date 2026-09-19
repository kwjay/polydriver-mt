from dataclasses import dataclass
from .constants import *


@dataclass(frozen=True)
class ResponseFrame:
	source_id: int
	command: int
	length: int
	payload: bytes


def calculate_crc8(data: bytes) -> int:
    crc = 0x00
    for byte in data:
        extract = byte
        for _ in range(8):
            sum_bit = (crc ^ extract) & 0x01
            crc >>= 1
            if sum_bit:
                crc ^= 0x8C
            extract >>= 1
    return crc


def build_frame(target_id: int, cmd: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD_LEN:
        raise ValueError(
            f"payload length {len(payload)} exceeds MAX_PAYLOAD_LEN ({MAX_PAYLOAD_LEN})"
        )
    length = len(payload)
    crc_data = bytes([target_id, cmd, length]) + payload
    crc = calculate_crc8(crc_data)
    return bytes([STX]) + crc_data + bytes([crc, ETX])


class ProtocolParser:
    def __init__(self):
        self.state = RxState.WAIT_STX
        self.rx_id = 0
        self.rx_command = 0
        self.rx_length = 0
        self.rx_payload = bytearray()
        self.rx_crc = 0

    def process_byte(self, b: int) -> ResponseFrame | None:
        if self.state == RxState.WAIT_STX:
            if b == STX:
                self.state = RxState.READ_ID
                self.rx_payload.clear()

        elif self.state == RxState.READ_ID:
            self.rx_id = b
            self.state = RxState.READ_CMD

        elif self.state == RxState.READ_CMD:
            self.rx_command = b
            self.state = RxState.READ_LEN

        elif self.state == RxState.READ_LEN:
            self.rx_length = b
            if self.rx_length > MAX_PAYLOAD_LEN:
                self.state = RxState.WAIT_STX
            else:
                self.state = (
                    RxState.READ_PAYLOAD if self.rx_length > 0 else RxState.READ_CRC
                )

        elif self.state == RxState.READ_PAYLOAD:
            self.rx_payload.append(b)
            if len(self.rx_payload) >= self.rx_length:
                self.state = RxState.READ_CRC

        elif self.state == RxState.READ_CRC:
            self.rx_crc = b
            self.state = RxState.WAIT_ETX

        elif self.state == RxState.WAIT_ETX:
            self.state = RxState.WAIT_STX
            if b == ETX:
                return self._process_frame()
        return None

    def _process_frame(self) -> ResponseFrame | None:
        crc_data = bytes([self.rx_id, self.rx_command, self.rx_length]) + bytes(
            self.rx_payload
        )
        calculated_crc = calculate_crc8(crc_data)

        if calculated_crc != self.rx_crc:
            return None

        return ResponseFrame(
            source_id=self.rx_id,
            command=self.rx_command,
            length=self.rx_length,
            payload=bytes(self.rx_payload),
        )