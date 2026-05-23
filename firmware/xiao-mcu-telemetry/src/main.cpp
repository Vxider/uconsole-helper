#include <Arduino.h>
#include <Adafruit_LittleFS.h>
#include <InternalFileSystem.h>
#include <LSM6DS3.h>
#include <PDM.h>
#include <Wire.h>
#include <math.h>

using namespace Adafruit_LittleFS_Namespace;

LSM6DS3 imu(I2C_MODE, 0x6A);

static const uint32_t SAMPLE_INTERVAL_MS = 50;
static const uint32_t HEARTBEAT_INTERVAL_MS = 30000;
static const uint32_t LONG_STILL_HEARTBEAT_INTERVAL_MS = 120000;
static const uint32_t SCREEN_OFF_HEARTBEAT_INTERVAL_MS = 300000;
static const uint32_t LONG_STILL_MS = 120000;
static const uint32_t STATUS_MIN_INTERVAL_MS = 250;
static const uint32_t LIGHT_REPORT_FAST_INTERVAL_MS = 1000;
static const uint32_t LIGHT_REPORT_SLOW_INTERVAL_MS = 30000;
static const uint32_t LIGHT_REPORT_STABLE_MS = 800;
static const uint32_t LIGHT_REPORT_BOOT_GRACE_MS = 5000;
static const uint32_t ORIENTATION_STABLE_MS = 700;
static const uint32_t PICKUP_REST_MIN_MS = 900;
static const uint32_t PICKUP_SEQUENCE_WINDOW_MS = 1600;
static const uint32_t PICKUP_CONFIRM_MS = 300;
static const uint32_t PUTDOWN_STABLE_MS = 1800;
static const uint32_t PUTDOWN_WITH_EVIDENCE_STABLE_MS = 900;
static const uint32_t PUTDOWN_SOUND_WINDOW_MS = 900;
static const uint32_t PUTDOWN_EVIDENCE_WINDOW_MS = 1600;
static const uint32_t MIC_ASSIST_WINDOW_MS = 1800;
static const float PICKUP_DELTA_G = 0.10f;
static const float PICKUP_START_DELTA_G = 0.16f;
static const float PICKUP_CONFIRM_DELTA_G = 0.08f;
static const float PICKUP_GYRO_DPS = 45.0f;
static const float PICKUP_ORIENTATION_CHANGE_G = 0.22f;
static const float PUTDOWN_DELTA_G = 0.055f;
static const float PUTDOWN_DELTA_WITH_EVIDENCE_G = 0.085f;
static const float G_CHANGE_DELTA = 0.14f;
static const float FREEFALL_G = 0.35f;
static const float IMPACT_G = 1.85f;
static const float PUTDOWN_IMPACT_G = 1.35f;
static const float ORIENTATION_AXIS_G = 0.72f;
static const float STAND_MIN_Y_G = 0.35f;
static const float STAND_MIN_Z_G = 0.35f;
static const float STAND_MAX_X_G = 0.45f;
static const float STAND_MAX_DELTA_G = 0.035f;
static const float STAND_MAX_G_CHANGE = 0.12f;
static const int MIC_PEAK_THRESHOLD = 900;
static const uint8_t VEML7700_ADDR = 0x10;
static const uint8_t VEML7700_REG_ALS_CONF = 0x00;
static const uint8_t VEML7700_REG_ALS_DATA = 0x04;
static const uint16_t VEML7700_ALS_CONF = 0x0000;  // gain x1, 100 ms integration, ALS on.
static const float VEML7700_LUX_PER_COUNT = 0.0576f;
static const uint32_t LIGHT_SAMPLE_INTERVAL_MS = 1000;
static const float LIGHT_SMOOTH_ALPHA = 0.35f;
static const float LIGHT_REPORT_SMALL_DELTA_LUX = 6.0f;
static const float LIGHT_REPORT_LARGE_RATIO = 0.45f;
static const float LIGHT_PEAK_RATIO = 1.20f;
static const char *POSE_CALIBRATION_FILE = "/uconsole_pose.txt";

static uint32_t last_sample_ms = 0;
static uint32_t last_heartbeat_ms = 0;
static uint32_t last_light_sample_ms = 0;
static uint32_t last_light_report_ms = 0;
static uint32_t light_report_candidate_since = 0;
static uint32_t orientation_candidate_since = 0;
static uint32_t pickup_sequence_since = 0;
static uint32_t pickup_confirm_since = 0;
static uint32_t putdown_candidate_since = 0;
static uint32_t putdown_evidence_until = 0;
static uint32_t still_since = 0;
static bool imu_ready = false;
static bool mic_ready = false;
static bool mic_enabled = false;
static bool mic_assist_enabled = true;
static bool light_ready = false;
static bool motion_active = false;
static bool host_screen_on = true;
static bool stream_samples = false;
static String device_state = "held";

static String stable_orientation = "unknown";
static String resting_orientation = "unknown";
static String orientation_candidate = "unknown";
static float last_ax = 0.0f;
static float last_ay = 0.0f;
static float last_az = 1.0f;
static float pickup_start_ax = 0.0f;
static float pickup_start_ay = 0.0f;
static float pickup_start_az = 1.0f;
static bool have_last_sample = false;
static int16_t mic_buffer[256];
static volatile int mic_samples_read = 0;
static volatile int mic_peak = 0;
static volatile uint32_t mic_peak_ms = 0;
static int last_mic_peak = 0;
static uint32_t last_mic_peak_ms = 0;
static uint32_t mic_assist_until = 0;
static uint16_t light_raw = 0;
static float light_lux = 0.0f;
static float smoothed_light_lux = 0.0f;
static float last_reported_light_lux = -1.0f;
static float light_report_candidate_lux = 0.0f;
static bool light_sample_valid = false;
static bool have_smoothed_light = false;
static bool storage_ready = false;

struct Sample {
  float ax;
  float ay;
  float az;
  float gx;
  float gy;
  float gz;
  float g;
  float delta;
};

static void print_status(const char *event_name, const Sample &sample);

static bool valid_orientation_name(const String &name) {
  return name == "face_up" ||
         name == "face_down" ||
         name == "right_edge" ||
         name == "left_edge" ||
         name == "top_edge" ||
         name == "bottom_edge" ||
         name == "stand" ||
         name == "tilted";
}

static String read_saved_resting_orientation() {
  if (!storage_ready) {
    return "";
  }
  File file(InternalFS);
  if (!file.open(POSE_CALIBRATION_FILE, FILE_O_READ)) {
    return "";
  }
  char buffer[24] = {0};
  int read_len = file.read(buffer, sizeof(buffer) - 1);
  file.close();
  if (read_len <= 0) {
    return "";
  }
  buffer[read_len] = '\0';
  String value(buffer);
  value.trim();
  if (!valid_orientation_name(value)) {
    return "";
  }
  return value;
}

static bool save_resting_orientation(const String &orientation) {
  if (!storage_ready || !valid_orientation_name(orientation)) {
    return false;
  }
  File file(InternalFS);
  if (!file.open(POSE_CALIBRATION_FILE, FILE_O_WRITE)) {
    return false;
  }
  file.write(orientation.c_str(), orientation.length());
  file.close();
  return true;
}

static void on_pdm_data() {
  int bytes_available = PDM.available();
  if (bytes_available <= 0) {
    return;
  }
  if (bytes_available > (int)sizeof(mic_buffer)) {
    bytes_available = sizeof(mic_buffer);
  }
  PDM.read(mic_buffer, bytes_available);
  const int count = bytes_available / 2;
  int peak = 0;
  for (int i = 0; i < count; i++) {
    int value = abs((int)mic_buffer[i]);
    if (value > peak) {
      peak = value;
    }
  }
  mic_samples_read = count;
  mic_peak = peak;
  if (peak >= MIC_PEAK_THRESHOLD) {
    mic_peak_ms = millis();
  }
}

static void update_mic_state() {
  if (!mic_enabled) {
    last_mic_peak = 0;
    return;
  }
  noInterrupts();
  last_mic_peak = mic_peak;
  last_mic_peak_ms = mic_peak_ms;
  interrupts();
}

static void start_mic_assist(uint32_t now) {
  if (!mic_assist_enabled) {
    return;
  }
  mic_assist_until = now + MIC_ASSIST_WINDOW_MS;
  if (mic_enabled) {
    return;
  }
  noInterrupts();
  mic_peak = 0;
  mic_peak_ms = 0;
  interrupts();
  last_mic_peak = 0;
  last_mic_peak_ms = 0;
  mic_enabled = PDM.begin(1, 16000);
  mic_ready = mic_enabled;
}

static void update_mic_power(uint32_t now) {
  if (!mic_enabled) {
    return;
  }
  if (mic_assist_until > 0 && now <= mic_assist_until) {
    return;
  }
  PDM.end();
  mic_enabled = false;
  mic_ready = false;
}

static bool veml7700_write16(uint8_t reg, uint16_t value) {
  Wire.beginTransmission(VEML7700_ADDR);
  Wire.write(reg);
  Wire.write(value & 0xFF);
  Wire.write((value >> 8) & 0xFF);
  return Wire.endTransmission() == 0;
}

static bool veml7700_read16(uint8_t reg, uint16_t &value) {
  Wire.beginTransmission(VEML7700_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {
    return false;
  }
  if (Wire.requestFrom((int)VEML7700_ADDR, 2) != 2) {
    return false;
  }
  const uint8_t low = Wire.read();
  const uint8_t high = Wire.read();
  value = ((uint16_t)high << 8) | low;
  return true;
}

static bool init_light_sensor() {
  if (!veml7700_write16(VEML7700_REG_ALS_CONF, VEML7700_ALS_CONF)) {
    light_sample_valid = false;
    return false;
  }
  return true;
}

static void update_light_sensor(bool force) {
  const uint32_t now = millis();
  if (!force && now - last_light_sample_ms < LIGHT_SAMPLE_INTERVAL_MS) {
    return;
  }
  last_light_sample_ms = now;
  if (!light_ready) {
    light_ready = init_light_sensor();
    if (!light_ready) {
      return;
    }
    delay(3);
  }
  uint16_t value = 0;
  if (!veml7700_read16(VEML7700_REG_ALS_DATA, value)) {
    light_ready = false;
    light_sample_valid = false;
    return;
  }
  light_raw = value;
  light_lux = (float)value * VEML7700_LUX_PER_COUNT;
  if (!have_smoothed_light) {
    smoothed_light_lux = light_lux;
    have_smoothed_light = true;
  } else {
    smoothed_light_lux += LIGHT_SMOOTH_ALPHA * (light_lux - smoothed_light_lux);
  }
  light_sample_valid = true;
}

static float relative_delta(float a, float b) {
  const float base = max(max(a, b), 1.0f);
  return fabsf(a - b) / base;
}

static bool maybe_report_light_change(const Sample &sample, uint32_t now) {
  if (!host_screen_on && !stream_samples) {
    return false;
  }
  if (!light_ready || !light_sample_valid || !have_smoothed_light) {
    return false;
  }
  if (last_reported_light_lux < 0.0f) {
    last_reported_light_lux = smoothed_light_lux;
    last_light_report_ms = now;
    return false;
  }
  if (now < LIGHT_REPORT_BOOT_GRACE_MS) {
    return false;
  }

  const float raw_ratio = relative_delta(light_lux, smoothed_light_lux);
  if (raw_ratio >= LIGHT_PEAK_RATIO) {
    light_report_candidate_since = 0;
    return false;
  }

  const float delta = fabsf(smoothed_light_lux - last_reported_light_lux);
  const float ratio = relative_delta(smoothed_light_lux, last_reported_light_lux);
  const bool large_change = ratio >= LIGHT_REPORT_LARGE_RATIO || delta >= 80.0f;
  const bool small_change = delta >= LIGHT_REPORT_SMALL_DELTA_LUX;

  if (!small_change && now - last_light_report_ms < LIGHT_REPORT_SLOW_INTERVAL_MS) {
    return false;
  }
  if (!small_change) {
    last_reported_light_lux = smoothed_light_lux;
    last_light_report_ms = now;
    print_status("light_changed", sample);
    return true;
  }

  if (fabsf(smoothed_light_lux - light_report_candidate_lux) > LIGHT_REPORT_SMALL_DELTA_LUX) {
    light_report_candidate_lux = smoothed_light_lux;
    light_report_candidate_since = now;
    return false;
  }
  if (light_report_candidate_since == 0) {
    light_report_candidate_lux = smoothed_light_lux;
    light_report_candidate_since = now;
    return false;
  }

  const uint32_t min_interval = large_change ? LIGHT_REPORT_FAST_INTERVAL_MS : LIGHT_REPORT_SLOW_INTERVAL_MS;
  if (now - last_light_report_ms < min_interval) {
    return false;
  }
  if (large_change && now - light_report_candidate_since < LIGHT_REPORT_STABLE_MS) {
    return false;
  }
  if (!large_change && now - light_report_candidate_since < LIGHT_REPORT_STABLE_MS * 2) {
    return false;
  }

  last_reported_light_lux = smoothed_light_lux;
  last_light_report_ms = now;
  light_report_candidate_since = 0;
  print_status("light_changed", sample);
  return true;
}

static void mark_putdown_evidence(uint32_t now) {
  putdown_evidence_until = now + PUTDOWN_EVIDENCE_WINDOW_MS;
}

static Sample read_sample() {
  Sample sample;
  sample.ax = imu.readFloatAccelX();
  sample.ay = imu.readFloatAccelY();
  sample.az = imu.readFloatAccelZ();
  sample.gx = imu.readFloatGyroX();
  sample.gy = imu.readFloatGyroY();
  sample.gz = imu.readFloatGyroZ();
  sample.g = sqrtf(sample.ax * sample.ax + sample.ay * sample.ay + sample.az * sample.az);
  if (have_last_sample) {
    sample.delta = fabsf(sample.ax - last_ax) + fabsf(sample.ay - last_ay) + fabsf(sample.az - last_az);
  } else {
    sample.delta = 0.0f;
    have_last_sample = true;
  }
  last_ax = sample.ax;
  last_ay = sample.ay;
  last_az = sample.az;
  return sample;
}

static bool stand_pose_candidate(const Sample &sample) {
  return fabsf(sample.ax) <= STAND_MAX_X_G &&
         fabsf(sample.ay) >= STAND_MIN_Y_G &&
         fabsf(sample.az) >= STAND_MIN_Z_G;
}

static bool stand_still_candidate(const Sample &sample) {
  return stand_pose_candidate(sample) &&
         sample.delta <= STAND_MAX_DELTA_G &&
         fabsf(sample.g - 1.0f) <= STAND_MAX_G_CHANGE;
}

static void update_device_state(const Sample &sample) {
  if (motion_active) {
    device_state = "held";
    return;
  }
  if (stand_still_candidate(sample)) {
    device_state = "stand";
    return;
  }
  if (device_state != "put_down") {
    device_state = "held";
  }
}

static float orientation_change_since_pickup_start(const Sample &sample) {
  return fabsf(sample.ax - pickup_start_ax) +
         fabsf(sample.ay - pickup_start_ay) +
         fabsf(sample.az - pickup_start_az);
}

static bool pickup_start_candidate(const Sample &sample, uint32_t now) {
  if (still_since == 0 || now - still_since < PICKUP_REST_MIN_MS) {
    return false;
  }
  return sample.delta >= PICKUP_START_DELTA_G ||
         fabsf(sample.g - 1.0f) >= G_CHANGE_DELTA;
}

static bool pickup_confirm_candidate(const Sample &sample) {
  const float gyro_abs = max(max(fabsf(sample.gx), fabsf(sample.gy)), fabsf(sample.gz));
  const float orientation_change = orientation_change_since_pickup_start(sample);
  return orientation_change >= PICKUP_ORIENTATION_CHANGE_G ||
         gyro_abs >= PICKUP_GYRO_DPS ||
         sample.delta >= PICKUP_CONFIRM_DELTA_G;
}

static void reset_pickup_sequence() {
  pickup_sequence_since = 0;
  pickup_confirm_since = 0;
}

static String classify_orientation(const Sample &sample, bool allow_stand) {
  if (allow_stand && stand_still_candidate(sample)) {
    return "stand";
  }
  if (sample.az > ORIENTATION_AXIS_G) {
    return "face_up";
  }
  if (sample.az < -ORIENTATION_AXIS_G) {
    return "face_down";
  }
  if (sample.ax > ORIENTATION_AXIS_G) {
    return "right_edge";
  }
  if (sample.ax < -ORIENTATION_AXIS_G) {
    return "left_edge";
  }
  if (sample.ay > ORIENTATION_AXIS_G) {
    return "top_edge";
  }
  if (sample.ay < -ORIENTATION_AXIS_G) {
    return "bottom_edge";
  }
  return "tilted";
}

static void print_status(const char *event_name, const Sample &sample) {
  update_device_state(sample);
  Serial.print('{');
  Serial.print("\"event\":\"");
  Serial.print(event_name);
  Serial.print("\",\"state\":\"");
  Serial.print(device_state);
  Serial.print("\",\"pose\":\"");
  Serial.print(stable_orientation);
  Serial.print("\",\"motion\":\"");
  Serial.print(motion_active ? "moving" : "still");
  Serial.print("\",\"screen\":\"");
  Serial.print(host_screen_on ? "on" : "off");
  Serial.print("\",\"g\":");
  Serial.print(sample.g, 4);
  Serial.print(",\"delta\":");
  Serial.print(sample.delta, 4);
  Serial.print(",\"mic\":{\"ready\":");
  Serial.print(mic_ready ? "true" : "false");
  Serial.print(",\"assist\":");
  Serial.print(mic_assist_enabled ? "true" : "false");
  Serial.print(",\"enabled\":");
  Serial.print(mic_enabled ? "true" : "false");
  Serial.print(",\"peak\":");
  Serial.print(last_mic_peak);
  Serial.print(",\"recent_peak\":");
  Serial.print((millis() - last_mic_peak_ms <= PUTDOWN_SOUND_WINDOW_MS) ? "true" : "false");
  Serial.print("}");
  Serial.print(",\"light\":{\"ready\":");
  Serial.print(light_ready ? "true" : "false");
  Serial.print(",\"valid\":");
  Serial.print(light_sample_valid ? "true" : "false");
  Serial.print(",\"raw\":");
  Serial.print(light_raw);
  Serial.print(",\"lux\":");
  Serial.print(light_lux, 2);
  Serial.print(",\"smoothed_lux\":");
  Serial.print(smoothed_light_lux, 2);
  Serial.print("}");
  Serial.print(",\"accel\":{\"x\":");
  Serial.print(sample.ax, 4);
  Serial.print(",\"y\":");
  Serial.print(sample.ay, 4);
  Serial.print(",\"z\":");
  Serial.print(sample.az, 4);
  Serial.print("},\"gyro\":{\"x\":");
  Serial.print(sample.gx, 4);
  Serial.print(",\"y\":");
  Serial.print(sample.gy, 4);
  Serial.print(",\"z\":");
  Serial.print(sample.gz, 4);
  Serial.println("}}");
}

static void print_error(const char *error_name) {
  Serial.print("{\"error\":\"");
  Serial.print(error_name);
  Serial.println("\"}");
}

static void handle_command(const Sample &sample) {
  if (!Serial.available()) {
    return;
  }
  String command = Serial.readStringUntil('\n');
  command.trim();
  command.toLowerCase();
  if (command == "status" || command == "?" || command == "sample") {
    print_status("requested", sample);
  } else if (command == "calibrate pose" || command == "pose calibrate") {
    stable_orientation = classify_orientation(sample, true);
    resting_orientation = stable_orientation;
    orientation_candidate = stable_orientation;
    orientation_candidate_since = 0;
    reset_pickup_sequence();
    putdown_candidate_since = 0;
    motion_active = false;
    device_state = "held";
    save_resting_orientation(resting_orientation);
    print_status("pose_calibrated", sample);
  } else if (command == "stream on") {
    stream_samples = true;
    print_status("stream_on", sample);
  } else if (command == "stream off") {
    stream_samples = false;
    print_status("stream_off", sample);
  } else if (command == "screen off" || command == "display off") {
    host_screen_on = false;
    print_status("screen_off_ack", sample);
  } else if (command == "screen on" || command == "display on") {
    host_screen_on = true;
    print_status("screen_on_ack", sample);
  } else if (command == "mic assist on") {
    mic_assist_enabled = true;
    print_status("mic_assist_on", sample);
  } else if (command == "mic assist off") {
    mic_assist_enabled = false;
    mic_assist_until = 0;
    update_mic_power(millis());
    print_status("mic_assist_off", sample);
  } else if (command == "help") {
    Serial.println("{\"commands\":[\"status\",\"sample\",\"calibrate pose\",\"screen on\",\"screen off\",\"mic assist on\",\"mic assist off\",\"stream on\",\"stream off\",\"help\"]}");
  }
}

static uint32_t heartbeat_interval(uint32_t now) {
  if (!host_screen_on && !stream_samples) {
    return SCREEN_OFF_HEARTBEAT_INTERVAL_MS;
  }
  if (!motion_active && still_since > 0 && now - still_since >= LONG_STILL_MS) {
    return LONG_STILL_HEARTBEAT_INTERVAL_MS;
  }
  return HEARTBEAT_INTERVAL_MS;
}

void setup() {
  Serial.begin(115200);

  // Do not block on Serial. The GUI may open the port after boot.
  delay(300);

  storage_ready = InternalFS.begin();
  Wire.begin();
  imu_ready = (imu.begin() == 0);
  light_ready = init_light_sensor();
  update_light_sensor(true);
  PDM.onReceive(on_pdm_data);
  PDM.setGain(30);
  if (!imu_ready) {
    print_error("imu_init_failed");
  } else {
    Sample sample = read_sample();
    stable_orientation = classify_orientation(sample, true);
    String saved_resting_orientation = read_saved_resting_orientation();
    resting_orientation = saved_resting_orientation.length() > 0 ? saved_resting_orientation : stable_orientation;
    orientation_candidate = stable_orientation;
    still_since = millis();
    print_status("ready", sample);
  }
}

void loop() {
  const uint32_t now = millis();
  if (now - last_sample_ms < SAMPLE_INTERVAL_MS) {
    delay(2);
    return;
  }
  last_sample_ms = now;

  if (!imu_ready) {
    imu_ready = (imu.begin() == 0);
    if (!imu_ready) {
      print_error("imu_init_failed");
      return;
    }
  }

  Sample sample = read_sample();
  update_mic_power(now);
  update_mic_state();
  update_light_sensor(false);
  handle_command(sample);
  maybe_report_light_change(sample, now);

  if (stream_samples) {
    print_status("sample", sample);
    return;
  }

  if (sample.g < FREEFALL_G) {
    print_status("freefall", sample);
    return;
  }
  if (sample.g > IMPACT_G) {
    if (motion_active) {
      mark_putdown_evidence(now);
    }
    print_status("impact", sample);
    return;
  }

  const String previous_device_state = device_state;
  update_device_state(sample);

  const bool allow_stand_orientation = device_state == "stand";
  String current_orientation = classify_orientation(sample, allow_stand_orientation);
  if (current_orientation != orientation_candidate) {
    orientation_candidate = current_orientation;
    orientation_candidate_since = now;
  } else if (
      current_orientation != stable_orientation &&
      orientation_candidate_since > 0 &&
      now - orientation_candidate_since >= ORIENTATION_STABLE_MS) {
    stable_orientation = current_orientation;
    print_status("pose_changed", sample);
  }
  if (device_state != previous_device_state) {
    print_status("state_changed", sample);
  }

  const bool recent_sound_peak = last_mic_peak_ms > 0 && now - last_mic_peak_ms <= PUTDOWN_SOUND_WINDOW_MS;
  const bool putdown_impact = sample.g >= PUTDOWN_IMPACT_G;
  const bool putdown_mic_assist_candidate = motion_active &&
                                            (putdown_impact ||
                                             sample.delta <= PUTDOWN_DELTA_WITH_EVIDENCE_G);
  if (putdown_mic_assist_candidate) {
    start_mic_assist(now);
  }
  if (motion_active && (recent_sound_peak || putdown_impact)) {
    mark_putdown_evidence(now);
  }
  const bool recent_putdown_evidence = putdown_evidence_until > 0 && now <= putdown_evidence_until;
  const bool putdown_candidate = recent_putdown_evidence &&
                                 sample.delta <= PUTDOWN_DELTA_WITH_EVIDENCE_G &&
                                 fabsf(sample.g - 1.0f) <= G_CHANGE_DELTA &&
                                 stable_orientation != "tilted";
  const uint32_t required_putdown_stable_ms = PUTDOWN_WITH_EVIDENCE_STABLE_MS;

  if (!motion_active) {
    putdown_candidate_since = 0;
    if (pickup_sequence_since > 0 && now - pickup_sequence_since > PICKUP_SEQUENCE_WINDOW_MS) {
      reset_pickup_sequence();
    }
    if (pickup_sequence_since == 0) {
      if (pickup_start_candidate(sample, now)) {
        pickup_sequence_since = now;
        pickup_start_ax = sample.ax;
        pickup_start_ay = sample.ay;
        pickup_start_az = sample.az;
      }
    } else if (pickup_confirm_candidate(sample)) {
      if (pickup_confirm_since == 0) {
        pickup_confirm_since = now;
      } else if (now - pickup_confirm_since >= PICKUP_CONFIRM_MS) {
        motion_active = true;
        device_state = "held";
        reset_pickup_sequence();
        mic_assist_until = 0;
        update_mic_power(now);
        print_status("state_changed", sample);
        print_status("screen_wake_intent", sample);
        print_status("motion_started", sample);
      }
    } else {
      pickup_confirm_since = 0;
    }
  } else {
    reset_pickup_sequence();
    if (putdown_candidate) {
      if (putdown_candidate_since == 0) {
        putdown_candidate_since = now;
      } else if (now - putdown_candidate_since >= required_putdown_stable_ms) {
        motion_active = false;
        resting_orientation = stable_orientation;
        still_since = now;
        putdown_candidate_since = 0;
        putdown_evidence_until = 0;
        device_state = "put_down";
        print_status("state_changed", sample);
      }
    } else {
      putdown_candidate_since = 0;
    }
  }

  if (now - last_heartbeat_ms >= heartbeat_interval(now)) {
    last_heartbeat_ms = now;
    print_status("heartbeat", sample);
  }
}
