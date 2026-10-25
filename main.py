import M5
from hardware import Pin, PWM, I2C
import time
import math
import network
import ssl
import json
from umqtt.simple import MQTTClient

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
# PEMファイルの内容に置き換えてください
# ==========================================
ROOT_CA = ""

DEVICE_CERT = ""

PRIVATE_KEY = ""

# ==========================================
# 振動設定
# ==========================================
motor_pwm = PWM(Pin(26), freq=20000, duty=0)
HEARTBEAT_POWER = 400

TEMP_CHANNEL = 0

# MQTT受信データを保存する変数
pending_intervals = None

#動き検知用の状態管理
is_holding = False
last_motion_time = 0

def select_hub_channel(i2c, channel):
    if 0 <= channel <= 5:
        i2c.writeto(0x70, bytes([1 << channel]))
        time.sleep_ms(5)


def vibrate(power, duration_ms):
    motor_pwm.duty(power)
    time.sleep_ms(duration_ms)
    motor_pwm.duty(0)

def get_total_accel():
    """内蔵IMUから合成加速度(G)を取得する関数"""
    acc = M5.Imu.getAccel()
    # x, y, z 軸の合成ベクトル長を計算 (静止時は約 1.0G)
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
    global pending_intervals

    print("Received topic:", topic)
    print("Received message:", msg)

    try:
        data = json.loads(msg.decode())
        intervals = data.get("intervals")

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
            print("Rhythm received:", intervals)
        else:
            print("Invalid intervals")

    except Exception as e:
        print("JSON error:", e)


# ==========================================
# MQTT接続
# ==========================================
def connect_mqtt():
    # TLS設定の引数はMicroPythonの実装に依存します。
    # 使用環境がssl_paramsのcert/keyに対応しているか確認してください。
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
# 受信したリズムを再生
# intervalsは次の振動までの間隔（ms）
# ==========================================
def play_rhythm(intervals):
    global is_holding, last_motion_time

    for interval in intervals:
        M5.update()
        current_time = time.ticks_ms()
        accel_val = get_total_accel()

        if abs(accel_val - 1.0) > 0.25:
            last_motion_time = current_time

        # 1打目の振動：強めの振動
        vibrate(HEARTBEAT_POWER, 30)

        # 1打目と2打目の間のわずかなウェイト
        time.sleep_ms(50)

        # 2打目の振動：弱めの振動
        vibrate(int(HEARTBEAT_POWER *0.8), 40)

        # 次の振動までの残り時間
        consumed_time = 30 + 50 + 40
        remaining = max(0, interval - consumed_time)
        time.sleep_ms(remaining)

    motor_pwm.duty(0)


# ==========================================
# 温度測定（元の処理）
# ==========================================
def read_temperature(i2c):
    select_hub_channel(i2c, TEMP_CHANNEL)
    i2c.writeto(0x44, bytes([0x24, 0x00]))
    time.sleep_ms(20)

    data = i2c.readfrom(0x44, 6)
    raw_temp = (data[0] << 8) | data[1]

    return -45 + (175 * raw_temp / 65535)


# ==========================================
# メイン処理
# ==========================================
def setup():
    global pending_intervals, is_holding, last_motion_time

    M5.begin()
    motor_pwm.duty(0)

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

    connect_wifi()
    mqtt = connect_mqtt()

    last_temp_time = time.ticks_ms()

    while True:
        M5.update()
        current_time = time.ticks_ms()

        # MQTT受信を処理
        try:
            mqtt.check_msg()
        except Exception as e:
            print("MQTT receive error:", e)
            motor_pwm.duty(0)
            time.sleep(2)
            # 接続が切れた場合は、再接続が必要です
            mqtt = connect_mqtt()

        # --------------------------------------------------
        # 抱いた（動きが生じた）瞬間の判定
        # --------------------------------------------------
        accel_val = get_total_accel()
        if abs(accel_val - 1.0) > 0.25:
            last_motion_time = current_time
            if not is_holding:
                is_holding = True
                M5.Lcd.clear()
                M5.Lcd.setCursor(10, 10)
                M5.Lcd.print("Heartbeat ON")
        # --------------------------------------------------
        # 抱かれている間（is_holding == True）の処理
        # --------------------------------------------------
        if is_holding:
            # 新しいリズムを受信したら再生
            if pending_intervals is not None:
                intervals = pending_intervals
                pending_intervals = None
                play_rhythm(intervals)
            else:
                # リズムデータが来ていない場合の待機
                time.sleep_ms(100)

            # 最後に動きを検出してから8秒間静止していたら動作停止
            if time.ticks_diff(current_time, last_motion_time) > 8000:
                is_holding = False
                motor_pwm.duty(0)
                M5.Lcd.clear()
                M5.Lcd.setCursor(10, 10)
                M5.Lcd.print("Sleeping...")
        # --------------------------------------------------
        # 温度読み取り処理（2秒ごと）
        # --------------------------------------------------
        if time.ticks_diff(current_time, last_temp_time) >= 2000:
            try:
                temperature = read_temperature(i2c0)
                M5.Lcd.setCursor(10, 100)
                M5.Lcd.print(
                    "Temp: " + str(round(temperature, 1)) + " C   "
                )
            except Exception as e:
                print("Temperature error:", e)

            last_temp_time = current_time

        # スリープ時の検知ポーリング間隔
        if not is_holding:
            time.sleep_ms(200)

if __name__ == "__main__":
    setup()