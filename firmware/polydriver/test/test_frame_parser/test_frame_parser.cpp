#include <unity.h>
#include "FrameParser.h"
#include "Crc8.h"

using namespace ProtocolDefs;

void setUp(void) {}
void tearDown(void) {}

// Builds STX | ID | CMD | LEN | SEQ | payload | CRC | ETX into out; returns the frame length.
static uint8_t buildFrame(uint8_t* out, uint8_t command, uint8_t sequence,
                          const uint8_t* payload, uint8_t length, uint8_t id = BOARD_ID) {
  uint8_t n = 0;
  out[n++] = STX;
  out[n++] = id;
  out[n++] = command;
  out[n++] = length;
  out[n++] = sequence;
  for (uint8_t i = 0; i < length; i++) out[n++] = payload[i];
  out[n] = Crc8::compute(&out[1], static_cast<uint8_t>(n - 1));
  n++;
  out[n++] = ETX;
  return n;
}

// Feeds bytes 1 ms apart starting at startMs; returns the event of the last byte and counts
// how many FRAME / BAD_CRC events were seen in total.
struct FeedResult {
  ParseEvent last;
  uint8_t frames;
  uint8_t badCrcs;
};

static FeedResult feedAll(FrameParser& parser, const uint8_t* bytes, uint8_t count,
                          uint32_t startMs = 0, uint32_t stepMs = 1) {
  FeedResult result{ParseEvent::NONE, 0, 0};
  for (uint8_t i = 0; i < count; i++) {
    result.last = parser.feed(bytes[i], startMs + i * stepMs);
    if (result.last == ParseEvent::FRAME) result.frames++;
    if (result.last == ParseEvent::BAD_CRC) result.badCrcs++;
  }
  return result;
}

static void assertEvent(ParseEvent expected, ParseEvent actual) {
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(expected), static_cast<uint8_t>(actual));
}

static const uint8_t SPEED_100HZ[] = {0x00, 0x00, 0xC8, 0x42};

// --- Good frames ----------------------------------------------------------

void test_parser_reads_a_frame_with_payload() {
  FrameParser parser;
  uint8_t bytes[40];
  uint8_t n = buildFrame(bytes, 0x10, 7, SPEED_100HZ, 4);
  for (uint8_t i = 0; i + 1 < n; i++) assertEvent(ParseEvent::NONE, parser.feed(bytes[i], i));
  assertEvent(ParseEvent::FRAME, parser.feed(bytes[n - 1], n));

  const Frame& frame = parser.getFrame();
  TEST_ASSERT_EQUAL_HEX8(0x10, frame.command);
  TEST_ASSERT_EQUAL_UINT8(7, frame.sequence);
  TEST_ASSERT_EQUAL_UINT8(4, frame.length);
  TEST_ASSERT_EQUAL_HEX8_ARRAY(SPEED_100HZ, frame.payload, 4);
}

void test_parser_reads_a_frame_without_payload() {
  FrameParser parser;
  uint8_t bytes[40];
  uint8_t n = buildFrame(bytes, 0x30, 200, nullptr, 0);
  TEST_ASSERT_EQUAL_UINT8(7, n);
  assertEvent(ParseEvent::FRAME, feedAll(parser, bytes, n).last);
  TEST_ASSERT_EQUAL_HEX8(0x30, parser.getFrame().command);
  TEST_ASSERT_EQUAL_UINT8(200, parser.getFrame().sequence);
  TEST_ASSERT_EQUAL_UINT8(0, parser.getFrame().length);
}

void test_parser_accepts_the_maximum_payload() {
  FrameParser parser;
  uint8_t payload[MAX_REQUEST_PAYLOAD];
  for (uint8_t i = 0; i < MAX_REQUEST_PAYLOAD; i++) payload[i] = static_cast<uint8_t>(0xA0 + i);
  uint8_t bytes[40];
  uint8_t n = buildFrame(bytes, 0x72, 1, payload, MAX_REQUEST_PAYLOAD);
  assertEvent(ParseEvent::FRAME, feedAll(parser, bytes, n).last);
  TEST_ASSERT_EQUAL_HEX8_ARRAY(payload, parser.getFrame().payload, MAX_REQUEST_PAYLOAD);
}

void test_parser_payload_may_contain_stx_and_etx_bytes() {
  FrameParser parser;
  const uint8_t payload[] = {STX, ETX, STX, ETX};
  uint8_t bytes[40];
  uint8_t n = buildFrame(bytes, 0x10, 3, payload, 4);
  assertEvent(ParseEvent::FRAME, feedAll(parser, bytes, n).last);
  TEST_ASSERT_EQUAL_HEX8_ARRAY(payload, parser.getFrame().payload, 4);
}

void test_parser_unknown_opcodes_are_passed_on() {
  FrameParser parser;
  uint8_t bytes[40];
  uint8_t n = buildFrame(bytes, 0xEE, 9, nullptr, 0);
  assertEvent(ParseEvent::FRAME, feedAll(parser, bytes, n).last);
  TEST_ASSERT_EQUAL_HEX8(0xEE, parser.getFrame().command);
}

void test_parser_reads_back_to_back_frames() {
  FrameParser parser;
  uint8_t bytes[80];
  uint8_t n = buildFrame(bytes, 0x10, 1, SPEED_100HZ, 4);
  n = static_cast<uint8_t>(n + buildFrame(&bytes[n], 0x30, 2, nullptr, 0));
  FeedResult result = feedAll(parser, bytes, n);
  TEST_ASSERT_EQUAL_UINT8(2, result.frames);
  TEST_ASSERT_EQUAL_UINT8(2, parser.getFrame().sequence);
}

void test_parser_ignores_noise_before_a_frame() {
  FrameParser parser;
  uint8_t bytes[60] = {0xFF, 0x00, 0x03, 0x55, 0x01};
  uint8_t n = static_cast<uint8_t>(5 + buildFrame(&bytes[5], 0x30, 4, nullptr, 0));
  FeedResult result = feedAll(parser, bytes, n);
  TEST_ASSERT_EQUAL_UINT8(1, result.frames);
  TEST_ASSERT_EQUAL_UINT16(0, parser.getAbandonedFrames());
}

// --- Bad frames -----------------------------------------------------------

void test_parser_reports_a_bad_crc_with_command_and_sequence() {
  FrameParser parser;
  uint8_t bytes[40];
  uint8_t n = buildFrame(bytes, 0x10, 42, SPEED_100HZ, 4);
  bytes[n - 2] ^= 0x01;
  assertEvent(ParseEvent::BAD_CRC, feedAll(parser, bytes, n).last);
  TEST_ASSERT_EQUAL_HEX8(0x10, parser.getFrame().command);
  TEST_ASSERT_EQUAL_UINT8(42, parser.getFrame().sequence);
  TEST_ASSERT_EQUAL_UINT16(1, parser.getBadCrcFrames());
}

void test_parser_a_corrupted_payload_byte_is_a_bad_crc() {
  FrameParser parser;
  uint8_t bytes[40];
  uint8_t n = buildFrame(bytes, 0x10, 5, SPEED_100HZ, 4);
  bytes[6] ^= 0x40;
  assertEvent(ParseEvent::BAD_CRC, feedAll(parser, bytes, n).last);
}

void test_parser_a_corrupted_sequence_byte_is_a_bad_crc() {
  FrameParser parser;
  uint8_t bytes[40];
  uint8_t n = buildFrame(bytes, 0x30, 5, nullptr, 0);
  bytes[4] ^= 0x01;
  assertEvent(ParseEvent::BAD_CRC, feedAll(parser, bytes, n).last);
}

void test_parser_recovers_after_a_bad_crc() {
  FrameParser parser;
  uint8_t bytes[80];
  uint8_t n = buildFrame(bytes, 0x10, 1, SPEED_100HZ, 4);
  bytes[n - 2] ^= 0xFF;
  n = static_cast<uint8_t>(n + buildFrame(&bytes[n], 0x30, 2, nullptr, 0));
  FeedResult result = feedAll(parser, bytes, n);
  TEST_ASSERT_EQUAL_UINT8(1, result.badCrcs);
  TEST_ASSERT_EQUAL_UINT8(1, result.frames);
}

void test_parser_ignores_a_frame_for_another_board() {
  FrameParser parser;
  uint8_t bytes[80];
  uint8_t n = buildFrame(bytes, 0x10, 1, SPEED_100HZ, 4, 0x05);
  n = static_cast<uint8_t>(n + buildFrame(&bytes[n], 0x30, 2, nullptr, 0));
  FeedResult result = feedAll(parser, bytes, n);
  TEST_ASSERT_EQUAL_UINT8(1, result.frames);
  TEST_ASSERT_EQUAL_UINT8(0, result.badCrcs);
  TEST_ASSERT_EQUAL_UINT8(2, parser.getFrame().sequence);
  TEST_ASSERT_EQUAL_UINT16(0, parser.getAbandonedFrames());
}

void test_parser_a_repeated_stx_still_starts_the_frame() {
  FrameParser parser;
  uint8_t bytes[40] = {STX};
  uint8_t n = static_cast<uint8_t>(1 + buildFrame(&bytes[1], 0x30, 6, nullptr, 0));
  FeedResult result = feedAll(parser, bytes, n);
  TEST_ASSERT_EQUAL_UINT8(1, result.frames);
}

void test_parser_drops_a_length_above_the_limit() {
  FrameParser parser;
  uint8_t bytes[80] = {STX, BOARD_ID, 0x10, MAX_REQUEST_PAYLOAD + 1};
  uint8_t n = static_cast<uint8_t>(4 + buildFrame(&bytes[4], 0x30, 2, nullptr, 0));
  FeedResult result = feedAll(parser, bytes, n);
  TEST_ASSERT_EQUAL_UINT8(1, result.frames);
  TEST_ASSERT_EQUAL_UINT8(0, result.badCrcs);
  TEST_ASSERT_EQUAL_UINT16(1, parser.getAbandonedFrames());
}

void test_parser_drops_a_frame_without_etx() {
  FrameParser parser;
  uint8_t bytes[40];
  uint8_t n = buildFrame(bytes, 0x30, 2, nullptr, 0);
  bytes[n - 1] = 0x7E;
  FeedResult result = feedAll(parser, bytes, n);
  assertEvent(ParseEvent::NONE, result.last);
  TEST_ASSERT_EQUAL_UINT8(0, result.frames);
  TEST_ASSERT_EQUAL_UINT16(1, parser.getAbandonedFrames());
}

void test_parser_an_stx_in_place_of_etx_starts_the_next_frame() {
  FrameParser parser;
  uint8_t bytes[80];
  uint8_t n = buildFrame(bytes, 0x30, 1, nullptr, 0);
  n--;  // drop the ETX: the next frame's STX lands in its place
  n = static_cast<uint8_t>(n + buildFrame(&bytes[n], 0x30, 2, nullptr, 0));
  FeedResult result = feedAll(parser, bytes, n);
  TEST_ASSERT_EQUAL_UINT8(1, result.frames);
  TEST_ASSERT_EQUAL_UINT8(2, parser.getFrame().sequence);
  TEST_ASSERT_EQUAL_UINT16(1, parser.getAbandonedFrames());
}

// --- Inter-byte timeout ---------------------------------------------------

void test_parser_times_out_a_truncated_frame_and_accepts_the_next() {
  FrameParser parser;
  uint8_t first[40];
  uint8_t n = buildFrame(first, 0x10, 1, SPEED_100HZ, 4);
  feedAll(parser, first, 5, 0);  // STX ID CMD LEN SEQ, then the sender stops

  uint8_t second[40];
  uint8_t m = buildFrame(second, 0x30, 2, nullptr, 0);
  FeedResult result = feedAll(parser, second, m, 100);
  TEST_ASSERT_EQUAL_UINT8(1, result.frames);
  TEST_ASSERT_EQUAL_UINT8(2, parser.getFrame().sequence);
  TEST_ASSERT_EQUAL_UINT16(1, parser.getAbandonedFrames());
  (void)n;
}

void test_parser_a_gap_of_exactly_the_timeout_is_allowed() {
  FrameParser parser;
  uint8_t bytes[40];
  uint8_t n = buildFrame(bytes, 0x10, 1, SPEED_100HZ, 4);
  assertEvent(ParseEvent::FRAME, feedAll(parser, bytes, n, 0, INTER_BYTE_TIMEOUT_MS).last);
  TEST_ASSERT_EQUAL_UINT16(0, parser.getAbandonedFrames());
}

void test_parser_a_gap_just_over_the_timeout_abandons_the_frame() {
  FrameParser parser;
  uint8_t bytes[40];
  uint8_t n = buildFrame(bytes, 0x10, 1, SPEED_100HZ, 4);
  FeedResult result = feedAll(parser, bytes, n, 0, INTER_BYTE_TIMEOUT_MS + 1);
  TEST_ASSERT_EQUAL_UINT8(0, result.frames);
  TEST_ASSERT_TRUE(parser.getAbandonedFrames() >= 1);
}

void test_parser_idle_time_between_frames_is_not_a_timeout() {
  FrameParser parser;
  uint8_t bytes[40];
  uint8_t n = buildFrame(bytes, 0x30, 1, nullptr, 0);
  feedAll(parser, bytes, n, 0);
  assertEvent(ParseEvent::FRAME, feedAll(parser, bytes, n, 60000).last);
  TEST_ASSERT_EQUAL_UINT16(0, parser.getAbandonedFrames());
}

void test_parser_counts_one_timeout_per_abandoned_frame() {
  FrameParser parser;
  const uint8_t partial[] = {STX, BOARD_ID, 0x10, 0x04, 0x01, 0x00};
  feedAll(parser, partial, sizeof(partial), 0);
  feedAll(parser, partial, sizeof(partial), 1000);
  uint8_t bytes[40];
  uint8_t n = buildFrame(bytes, 0x30, 2, nullptr, 0);
  feedAll(parser, bytes, n, 2000);
  TEST_ASSERT_EQUAL_UINT16(2, parser.getAbandonedFrames());
}

void test_parser_timeout_survives_a_millis_wrap() {
  FrameParser parser;
  uint8_t bytes[40];
  uint8_t n = buildFrame(bytes, 0x10, 1, SPEED_100HZ, 4);
  assertEvent(ParseEvent::FRAME, feedAll(parser, bytes, n, 0xFFFFFFFCUL).last);
  TEST_ASSERT_EQUAL_UINT16(0, parser.getAbandonedFrames());
}

// Known limit (protocol.md section 2): when the next frame follows a truncated one within the
// timeout, its bytes complete the truncated frame and both are lost. The CRC catches it.
void test_parser_a_frame_arriving_inside_the_timeout_is_lost() {
  FrameParser parser;
  uint8_t first[40];
  buildFrame(first, 0x10, 1, SPEED_100HZ, 4);
  feedAll(parser, first, 6, 0);  // STX ID CMD LEN SEQ + 1 payload byte, then cut off

  uint8_t second[40];
  uint8_t m = buildFrame(second, 0x30, 2, nullptr, 0);
  FeedResult result = feedAll(parser, second, m, 7);
  TEST_ASSERT_EQUAL_UINT8(0, result.frames);
}

// --- Counters -------------------------------------------------------------

void test_parser_counters_saturate() {
  FrameParser parser;
  uint8_t bytes[40];
  uint8_t n = buildFrame(bytes, 0x30, 1, nullptr, 0);
  bytes[n - 2] ^= 0xFF;
  uint32_t t = 0;
  for (uint32_t i = 0; i < 70000UL; i++) {
    feedAll(parser, bytes, n, t);
    t += 100;
  }
  TEST_ASSERT_EQUAL_UINT16(0xFFFF, parser.getBadCrcFrames());
}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_parser_reads_a_frame_with_payload);
  RUN_TEST(test_parser_reads_a_frame_without_payload);
  RUN_TEST(test_parser_accepts_the_maximum_payload);
  RUN_TEST(test_parser_payload_may_contain_stx_and_etx_bytes);
  RUN_TEST(test_parser_unknown_opcodes_are_passed_on);
  RUN_TEST(test_parser_reads_back_to_back_frames);
  RUN_TEST(test_parser_ignores_noise_before_a_frame);
  RUN_TEST(test_parser_reports_a_bad_crc_with_command_and_sequence);
  RUN_TEST(test_parser_a_corrupted_payload_byte_is_a_bad_crc);
  RUN_TEST(test_parser_a_corrupted_sequence_byte_is_a_bad_crc);
  RUN_TEST(test_parser_recovers_after_a_bad_crc);
  RUN_TEST(test_parser_ignores_a_frame_for_another_board);
  RUN_TEST(test_parser_a_repeated_stx_still_starts_the_frame);
  RUN_TEST(test_parser_drops_a_length_above_the_limit);
  RUN_TEST(test_parser_drops_a_frame_without_etx);
  RUN_TEST(test_parser_an_stx_in_place_of_etx_starts_the_next_frame);
  RUN_TEST(test_parser_times_out_a_truncated_frame_and_accepts_the_next);
  RUN_TEST(test_parser_a_gap_of_exactly_the_timeout_is_allowed);
  RUN_TEST(test_parser_a_gap_just_over_the_timeout_abandons_the_frame);
  RUN_TEST(test_parser_idle_time_between_frames_is_not_a_timeout);
  RUN_TEST(test_parser_counts_one_timeout_per_abandoned_frame);
  RUN_TEST(test_parser_timeout_survives_a_millis_wrap);
  RUN_TEST(test_parser_a_frame_arriving_inside_the_timeout_is_lost);
  RUN_TEST(test_parser_counters_saturate);
  return UNITY_END();
}
