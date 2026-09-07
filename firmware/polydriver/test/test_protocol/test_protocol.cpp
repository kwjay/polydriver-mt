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
	uint8_t crcBuf[20];
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


void test_protocol_set_speed() {
	ProtocolHandler handler;
	handler.setTxCallback(dummyTxCallback);
	
	union { float f; uint8_t b[4]; } speedData;
	speedData.f = 15.5f;
	
	CommandEvent evt = pushValidFrame(handler, CMD_SET_SPEED, speedData.b, 4);
	
	TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::SPEED_UPDATED), static_cast<int>(evt));
	TEST_ASSERT_EQUAL_FLOAT(15.5f, handler.getSpeed());
	TEST_ASSERT_EQUAL_HEX8(RESP_ACK, txBuffer[2]); 
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
	TEST_ASSERT_EQUAL_HEX8(RESP_NACK, txBuffer[2]); 
}

void test_protocol_wrong_id_ignored() {
	ProtocolHandler handler;
	
	handler.processByte(STX);
	handler.processByte(0x99);
	
	handler.processByte(CMD_REQ_STAT);
	handler.processByte(0);
	handler.processByte(0x00);
	CommandEvent evt = handler.processByte(ETX);
	
	TEST_ASSERT_EQUAL(static_cast<int>(CommandEvent::NONE), static_cast<int>(evt));
}

int main(void) {
	UNITY_BEGIN();
	RUN_TEST(test_protocol_set_speed);
	RUN_TEST(test_protocol_set_pid);
	RUN_TEST(test_protocol_invalid_crc_sends_nack);
	RUN_TEST(test_protocol_wrong_id_ignored);
	return UNITY_END();
}