#include <unity.h>
#include <cstring>
#include "protocol_handler.h"

uint8_t txBuffer[64];
uint8_t txLength = 0;

void dummyTxCallback(const uint8_t* data, uint8_t len) {
	for(uint8_t i = 0; i < len; i++) {
		txBuffer[i] = data[i];
	}
	txLength = len;
}

void setUp(void) {
	txLength = 0;
	memset(txBuffer, 0, sizeof(txBuffer));
}

void tearDown(void) {
}

uint8_t calculateTestCRC8(const uint8_t* data, uint8_t len) {
	uint8_t crc = 0x00;
	for (uint8_t i = 0; i < len; i++) {
			uint8_t extract = data[i];
			for (uint8_t tempI = 8; tempI; tempI--) {
				uint8_t sum = (crc ^ extract) & 0x01;
				crc >>= 1;
				if (sum) crc ^= 0x8C;
				extract >>= 1;
			}
	}
	return crc;
}

CommandEvent pushValidFrame(ProtocolHandler& handler, uint8_t cmd, const uint8_t* payload, uint8_t len) {
	uint8_t crcBuf[3 + MAX_PAYLOAD_LEN];
	crcBuf[0] = MY_ID;
	crcBuf[1] = cmd;
	crcBuf[2] = len;
	if (len > 0) memcpy(&crcBuf[3], payload, len);

	uint8_t crc = calculateTestCRC8(crcBuf, 3 + len);

	handler.processByte(STX);
	handler.processByte(MY_ID);
	handler.processByte(cmd);
	handler.processByte(len);
	for(uint8_t i = 0; i < len; i++) {
		handler.processByte(payload[i]);
	}
	handler.processByte(crc);

	return handler.processByte(ETX);
}

// STX | ID | CMD | LEN | payload... | CRC | ETX
uint8_t responseCommand() { return txBuffer[2]; }
uint8_t responsePayloadLength() { return txBuffer[3]; }
uint8_t nackReason() { return txBuffer[4]; }


void test_protocol_set_speed() {
	ProtocolHandler handler;
	handler.setTxCallback(dummyTxCallback);

	union { float f; uint8_t b[4]; } speedData;
	speedData.f = 15.5f;

	CommandEvent evt = pushValidFrame(handler, CMD_SET_SPEED, speedData.b, 4);

	TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::SPEED_UPDATED), static_cast<int>(evt));
	TEST_ASSERT_EQUAL_FLOAT(15.5f, handler.getSpeed());
	TEST_ASSERT_EQUAL_HEX8(RESP_ACK, responseCommand());
}

void test_protocol_set_pid() {
	ProtocolHandler handler;

	uint8_t payload[12];
	union { float f; uint8_t b[4]; } conv;

	conv.f = 1.0f; memcpy(&payload[0], conv.b, 4);
	conv.f = 0.5f; memcpy(&payload[4], conv.b, 4);
	conv.f = 0.1f; memcpy(&payload[8], conv.b, 4);

	CommandEvent evt = pushValidFrame(handler, CMD_SET_PID, payload, 12);

	TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::PID_UPDATED), static_cast<int>(evt));

	const PidSettings& pid = handler.getPidSettings();
	TEST_ASSERT_EQUAL_FLOAT(1.0f, pid.kp);
	TEST_ASSERT_EQUAL_FLOAT(0.5f, pid.ki);
	TEST_ASSERT_EQUAL_FLOAT(0.1f, pid.kd);
}

void test_protocol_invalid_crc_sends_nack() {
	ProtocolHandler handler;
	handler.setTxCallback(dummyTxCallback);

	handler.processByte(STX);
	handler.processByte(MY_ID);
	handler.processByte(CMD_REQ_STAT);
	handler.processByte(0);
	handler.processByte(0xFF);

	CommandEvent evt = handler.processByte(ETX);

	TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::NONE), static_cast<int>(evt));
	TEST_ASSERT_TRUE(txLength > 0);
	TEST_ASSERT_EQUAL_HEX8(RESP_NACK, responseCommand());
}

void test_protocol_nack_reports_bad_crc_reason() {
	ProtocolHandler handler;
	handler.setTxCallback(dummyTxCallback);

	handler.processByte(STX);
	handler.processByte(MY_ID);
	handler.processByte(CMD_REQ_STAT);
	handler.processByte(0);
	handler.processByte(0xFF);
	handler.processByte(ETX);

	TEST_ASSERT_EQUAL_HEX8(RESP_NACK, responseCommand());
	TEST_ASSERT_EQUAL_UINT8(1, responsePayloadLength());
	TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(NackReason::BAD_CRC), nackReason());
}

void test_protocol_wrong_id_is_ignored_silently_and_the_parser_recovers() {
	ProtocolHandler handler;
	handler.setTxCallback(dummyTxCallback);

	handler.processByte(STX);
	handler.processByte(0x99);
	handler.processByte(CMD_REQ_STAT);
	handler.processByte(0);
	handler.processByte(0x00);
	CommandEvent evt = handler.processByte(ETX);

	TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::NONE), static_cast<int>(evt));
	TEST_ASSERT_EQUAL_UINT8(0, txLength);

	CommandEvent ours = pushValidFrame(handler, CMD_REQ_STAT, nullptr, 0);
	TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::TELEMETRY_REQUESTED), static_cast<int>(ours));
}

void test_protocol_recovers_from_corrupt_length_byte() {
	ProtocolHandler handler;
	handler.setTxCallback(dummyTxCallback);
	handler.processByte(STX);
	handler.processByte(MY_ID);
	handler.processByte(CMD_REQ_STAT);
	handler.processByte(200);

	for (uint8_t i = 0; i < 50; i++) {
		CommandEvent evt = handler.processByte(i);
		TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::NONE), static_cast<int>(evt));
	}
	CommandEvent evt = pushValidFrame(handler, CMD_REQ_STAT, nullptr, 0);
	TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::TELEMETRY_REQUESTED), static_cast<int>(evt));
}

void test_protocol_bad_payload_length_sends_nack() {
	ProtocolHandler handler;
	handler.setTxCallback(dummyTxCallback);
	uint8_t payload[2] = {0x00, 0x01};
	CommandEvent evt = pushValidFrame(handler, CMD_SET_SPEED, payload, 2);

	TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::NONE), static_cast<int>(evt));
	TEST_ASSERT_TRUE(txLength > 0);
	TEST_ASSERT_EQUAL_HEX8(RESP_NACK, responseCommand());
	TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(NackReason::BAD_LENGTH), nackReason());
}

void test_protocol_set_pid_with_bad_payload_length_sends_nack() {
	ProtocolHandler handler;
	handler.setTxCallback(dummyTxCallback);
	uint8_t payload[8] = {0};
	CommandEvent evt = pushValidFrame(handler, CMD_SET_PID, payload, 8);

	TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::NONE), static_cast<int>(evt));
	TEST_ASSERT_EQUAL_HEX8(RESP_NACK, responseCommand());
	TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(NackReason::BAD_LENGTH), nackReason());
}

void test_protocol_unknown_command_sends_nack() {
	ProtocolHandler handler;
	handler.setTxCallback(dummyTxCallback);

	CommandEvent evt = pushValidFrame(handler, 0x7F, nullptr, 0);

	TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::NONE), static_cast<int>(evt));
	TEST_ASSERT_TRUE(txLength > 0);
	TEST_ASSERT_EQUAL_HEX8(RESP_NACK, responseCommand());
	TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(NackReason::UNKNOWN_COMMAND), nackReason());
}

void test_protocol_req_stat_with_a_payload_is_rejected() {
	ProtocolHandler handler;
	handler.setTxCallback(dummyTxCallback);
	uint8_t payload[2] = {0xAA, 0xBB};

	CommandEvent evt = pushValidFrame(handler, CMD_REQ_STAT, payload, 2);

	TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::NONE), static_cast<int>(evt));
	TEST_ASSERT_EQUAL_HEX8(RESP_NACK, responseCommand());
	TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(NackReason::BAD_LENGTH), nackReason());
}

void test_protocol_req_settings_with_a_payload_is_rejected() {
	ProtocolHandler handler;
	handler.setTxCallback(dummyTxCallback);
	uint8_t payload[1] = {0xAA};

	CommandEvent evt = pushValidFrame(handler, CMD_REQ_SETTINGS, payload, 1);

	TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::NONE), static_cast<int>(evt));
	TEST_ASSERT_EQUAL_HEX8(RESP_NACK, responseCommand());
	TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(NackReason::BAD_LENGTH), nackReason());
}

void test_protocol_req_settings_without_payload_still_works() {
	ProtocolHandler handler;
	handler.setTxCallback(dummyTxCallback);

	CommandEvent evt = pushValidFrame(handler, CMD_REQ_SETTINGS, nullptr, 0);

	TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::SYNC_REQUESTED), static_cast<int>(evt));
	TEST_ASSERT_EQUAL_UINT8(0, txLength);
}

void test_protocol_a_rejected_frame_does_not_disturb_the_next_one() {
	ProtocolHandler handler;
	handler.setTxCallback(dummyTxCallback);

	pushValidFrame(handler, 0x7F, nullptr, 0);
	uint8_t shortPayload[2] = {0x00, 0x01};
	pushValidFrame(handler, CMD_SET_SPEED, shortPayload, 2);

	union { float f; uint8_t b[4]; } speedData;
	speedData.f = 42.0f;
	CommandEvent evt = pushValidFrame(handler, CMD_SET_SPEED, speedData.b, 4);

	TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::SPEED_UPDATED), static_cast<int>(evt));
	TEST_ASSERT_EQUAL_FLOAT(42.0f, handler.getSpeed());
	TEST_ASSERT_EQUAL_HEX8(RESP_ACK, responseCommand());
}

void test_protocol_max_length_payload_is_accepted_by_the_framer() {
	ProtocolHandler handler;
	handler.setTxCallback(dummyTxCallback);
	uint8_t payload[MAX_PAYLOAD_LEN];
	memset(payload, 0x5A, sizeof(payload));

	CommandEvent evt = pushValidFrame(handler, 0x7F, payload, MAX_PAYLOAD_LEN);

	TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::NONE), static_cast<int>(evt));
	TEST_ASSERT_EQUAL_HEX8(RESP_NACK, responseCommand());
	TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(NackReason::UNKNOWN_COMMAND), nackReason());
}

int main(void) {
	UNITY_BEGIN();
	RUN_TEST(test_protocol_set_speed);
	RUN_TEST(test_protocol_set_pid);
	RUN_TEST(test_protocol_invalid_crc_sends_nack);
	RUN_TEST(test_protocol_nack_reports_bad_crc_reason);
	RUN_TEST(test_protocol_wrong_id_is_ignored_silently_and_the_parser_recovers);
	RUN_TEST(test_protocol_recovers_from_corrupt_length_byte);
	RUN_TEST(test_protocol_bad_payload_length_sends_nack);
	RUN_TEST(test_protocol_set_pid_with_bad_payload_length_sends_nack);
	RUN_TEST(test_protocol_unknown_command_sends_nack);
	RUN_TEST(test_protocol_req_stat_with_a_payload_is_rejected);
	RUN_TEST(test_protocol_req_settings_with_a_payload_is_rejected);
	RUN_TEST(test_protocol_req_settings_without_payload_still_works);
	RUN_TEST(test_protocol_a_rejected_frame_does_not_disturb_the_next_one);
	RUN_TEST(test_protocol_max_length_payload_is_accepted_by_the_framer);
	return UNITY_END();
}
