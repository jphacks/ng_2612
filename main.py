import M5
from hardware import Pin, I2C
import time
import math
import network
import ssl
import json
from umqtt.simple import MQTTClient
from unit import RGBUnit

# ==========================================
# Wi-Fi設定
# ==========================================
WIFI_SSID = ""
WIFI_PASSWORD = ""

# ==========================================
# AWS IoT Core設定
# ==========================================
AWS_ENDPOINT = "a1raisll7u0wql-ats.iot.ap-northeast-1.amazonaws.com"
CLIENT_ID = "Heartbeat"
MQTT_TOPIC = b"heartbeat/play"
MQTT_PORT = 8883

# ==========================================
# 証明書設定
# ==========================================
ROOT_CA = ""
DEVICE_CERT = ""
PRIVATE_KEY = ""

# ==========================================
# Hub・センサー・アクチュエーター設定
# ==========================================
PAHUB_ADDR = 0x70
PBHUB_ADDR = 0x61
SHT30_ADDR = 0x44

TEMP_CHANNEL = 0
HUB_DEVICE_CHANNEL = 1  # PbHubが繋がっているPaHubのチャンネル

# PbHub上のポート割り当て
RELAY_COMMAND = 0x41
MOTOR_COMMAND = 0x42

TARGET_TEMP_LOW = 37.0
TARGET_TEMP_HIGH = 38.0

ON_LEVEL = 1
OFF_LEVEL = 0

heater_is_on = False
pending_intervals = None
pending_color = 0xFF0000  # デフォルトの色（赤）

# 動き検知用の状態管理
is_holding = False
last_motion_time = 0

rgb_0 = RGBUnit((36, 26), 3)
rgb_0.set_brightness(20)

def select_hub_channel(i2c, channel):
    if 0 <= channel <= 5:
        i2c.writeto(PAHUB_ADDR, bytes([1 << channel]))
        time.sleep_ms(5)

def set_pbhub_port(i2c, port_command, level):
    """PbHubの指定ポート（コマンド）に対してON/OFFレベルを送信する"""
    select_hub_channel(i2c, HUB_DEVICE_CHANNEL)
    i2c.writeto(PBHUB_ADDR, bytes([port_command, level]))
    time.sleep_ms(20)

def vibrate(i2c, duration_ms):
    """PbHub経由で振動モーターをON/OFFさせる"""
    set_pbhub_port(i2c, MOTOR_COMMAND, ON_LEVEL)
    time.sleep_ms(duration_ms)
    set_pbhub_port(i2c, MOTOR_COMMAND, OFF_LEVEL)

def get_total_accel():
    """内蔵IMUから合成加速度(G)を取得する関数"""
    acc = M5.Imu.getAccel()
    return math.sqrt(acc[0]**2 + acc[1]**2 + acc[2]**2)

# ==========================================
# Wi-Fi接続
# ==========================================
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if not wlan.isconnected():
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        timeout = 20
        while not wlan.isconnected() and timeout > 0:
            time.sleep(1)
            timeout -= 1

    if not wlan.isconnected():
        raise RuntimeError("Wi-Fi connection failed")

    print("Wi-Fi connected:", wlan.ifconfig()[0])
    return wlan

# ==========================================
# MQTTメッセージ受信
# ==========================================
def mqtt_callback(topic, msg):
    global pending_intervals, pending_color
    print("Received topic:", topic)
    print("Received message:", msg)

    try:
        data = json.loads(msg.decode())
        intervals = data.get("intervals")
        color_hex = data.get("color")

        if color_hex and isinstance(color_hex, str) and color_hex.startswith("#"):
            try:
                pending_color = int(color_hex.lstrip("#"), 16)
            except Exception:
                pass

        if (
            isinstance(intervals, list)
            and len(intervals) == 9
            and all(
                isinstance(x, int)
                and not isinstance(x, bool)
                and 100 <= x <= 5000
                for x in intervals
            )
        ):
            pending_intervals = intervals
            print("Rhythm received:", intervals, "Color:", color_hex)
        else:
            print("Invalid intervals")

    except Exception as e:
        print("JSON error:", e)

# ==========================================
# MQTT接続
# ==========================================
def connect_mqtt():
    tls_params = {
        "key": PRIVATE_KEY,
        "cert": DEVICE_CERT,
        "server_hostname": AWS_ENDPOINT
    }

    client = MQTTClient(
        client_id=CLIENT_ID,
        server=AWS_ENDPOINT,
        port=MQTT_PORT,
        keepalive=60,
        ssl=True,
        ssl_params=tls_params
    )

    client.set_callback(mqtt_callback)
    client.connect()
    client.subscribe(MQTT_TOPIC)

    print("MQTT connected")
    print("Subscribed:", MQTT_TOPIC)
    return client

# ==========================================
# 受信したリズムを繰り返し再生（PbHub経由の振動）
# ==========================================
def play_rhythm(i2c, intervals, color_val, repeat_count=2):
    global last_motion_time

    rgb_0.fill_color(color_val)

    if intervals and len(intervals) > 0:
        avg_interval = int(sum(intervals) / len(intervals))
    else:
        avg_interval = 800

    for rep in range(repeat_count):
        for interval in intervals:
            M5.update()
            current_time = time.ticks_ms()
            accel_val = get_total_accel()

            if abs(accel_val - 1.0) > 0.25:
                last_motion_time = current_time

            # 1打目の振動（トッ：30ms）
            vibrate(i2c, 30)
            time.sleep_ms(50)
            # 2打目の振動（クン：40ms）
            vibrate(i2c, 40)

            consumed_time = 30 + 50 + 40
            remaining = max(0, interval - consumed_time)
            time.sleep_ms(remaining)
        
        if rep < repeat_count - 1:
            time.sleep_ms(avg_interval)

    # 念のため停止指令
    set_pbhub_port(i2c, MOTOR_COMMAND, OFF_LEVEL)

# ==========================================
# 温度測定
# ==========================================
def read_temperature(i2c):
    select_hub_channel(i2c, TEMP_CHANNEL)
    i2c.writeto(SHT30_ADDR, bytes([0x24, 0x00]))
    time.sleep_ms(20)

    data = i2c.readfrom(SHT30_ADDR, 6)
    raw_temp = (data[0] << 8) | data[1]
    return -45 + (175 * raw_temp / 65535)

# ==========================================
# 画面描画ヘルパー
# ==========================================
def update_lcd(status_text, temp_val, heater_status):
    M5.Lcd.clear()
    M5.Lcd.setTextSize(2)
    M5.Lcd.setCursor(10, 10)
    M5.Lcd.print(status_text)
    
    M5.Lcd.setCursor(10, 50)
    M5.Lcd.print(f"Temp: {round(temp_val, 1)} C")

    M5.Lcd.setCursor(10, 90)
    M5.Lcd.print("Heater: " + heater_status)

# ==========================================
# メイン処理
# ==========================================
def setup():
    global pending_intervals, pending_color, is_holding, last_motion_time, heater_is_on

    M5.begin()

    # 起動時はLED消灯
    rgb_0.fill_color(0x000000)

    M5.Lcd.setTextSize(3)
    M5.Lcd.clear()
    M5.Lcd.setCursor(10, 10)
    M5.Lcd.print("AWS Heartbeat")

    i2c0 = I2C(
        0,
        scl=Pin(33),
        sda=Pin(32),
        freq=100000
    )

    # 起動時は安全のためヒーターとモーターをOFFにする
    set_pbhub_port(i2c0, RELAY_COMMAND, OFF_LEVEL)
    set_pbhub_port(i2c0, MOTOR_COMMAND, OFF_LEVEL)

    connect_wifi()
    mqtt = connect_mqtt()

    last_temp_time = time.ticks_ms()
    current_temp = 0.0

    while True:
        M5.update()
        current_time = time.ticks_ms()

        # MQTT受信を処理
        try:
            mqtt.check_msg()
        except Exception as e:
            print("MQTT receive error:", e)
            set_pbhub_port(i2c0, MOTOR_COMMAND, OFF_LEVEL)
            time.sleep(2)
            mqtt = connect_mqtt()

        # --------------------------------------------------
        # 1. 揺れ（加速度）の検知
        # --------------------------------------------------
        accel_val = get_total_accel()
        if abs(accel_val - 1.0) > 0.25:
            last_motion_time = current_time
            if not is_holding:
                is_holding = True
                rgb_0.fill_color(pending_color)
                print("Heartbeat & Heater & Light ON")

        # --------------------------------------------------
        # 2. 状態に応じた処理（Active / Sleeping）
        # --------------------------------------------------
        if is_holding:
            # リズムデータがあれば再生
            if pending_intervals is not None:
                intervals = pending_intervals
                color_val = pending_color
                pending_intervals = None
                play_rhythm(i2c0, intervals, color_val, repeat_count=2)

            # 8秒間静止していたらスリープに戻す
            if time.ticks_diff(current_time, last_motion_time) > 8000:
                is_holding = False
                set_pbhub_port(i2c0, MOTOR_COMMAND, OFF_LEVEL)

                if heater_is_on:
                    set_pbhub_port(i2c0, RELAY_COMMAND, OFF_LEVEL)
                    heater_is_on = False

                rgb_0.fill_color(0x000000)

                M5.Lcd.clear()
                M5.Lcd.setTextSize(3)
                M5.Lcd.setCursor(10, 10)
                M5.Lcd.print("AWS Heartbeat")

        # --------------------------------------------------
        # 3. 温度読み取り＆ヒーター制御（2秒ごと）
        # --------------------------------------------------
        if time.ticks_diff(current_time, last_temp_time) >= 2000:
            try:
                current_temp = read_temperature(i2c0)
                if is_holding:
                    if current_temp < TARGET_TEMP_LOW and not heater_is_on:
                        set_pbhub_port(i2c0, RELAY_COMMAND, ON_LEVEL)
                        heater_is_on = True
                        print("Relay ON command")
                    elif current_temp > TARGET_TEMP_HIGH and heater_is_on:
                        set_pbhub_port(i2c0, RELAY_COMMAND, OFF_LEVEL)
                        heater_is_on = False
                        print("Relay OFF command")
                else:
                    if heater_is_on:
                        set_pbhub_port(i2c0, RELAY_COMMAND, OFF_LEVEL)
                        heater_is_on = False
                        print("Relay OFF command (sleeping)")

            except Exception as e:
                print("Temperature error:", e)
                try:
                    set_pbhub_port(i2c0, RELAY_COMMAND, OFF_LEVEL)
                except Exception:
                    pass
                heater_is_on = False

            last_temp_time = current_time

        # --------------------------------------------------
        # 4. 画面表示の更新
        # --------------------------------------------------
        if is_holding:
            update_lcd("Status: Active", current_temp, "ON" if heater_is_on else "OFF")

        # スリープ時の待機
        if not is_holding:
            time.sleep_ms(200)
        else:
            time.sleep_ms(50)

if __name__ == "__main__":
    setup()