import M5
from hardware import Pin, PWM, I2C
import time
import network
import ssl
import json
from umqtt.simple import MQTTClient

# ==========================================
# 設定ファイルと証明書のパス
# ==========================================
WIFI_CONFIG_FILE = "/wifi.json"
ROOT_CA_FILE = "/AmazonRootCA1.pem"
DEVICE_CERT_FILE = "/device-certificate.pem.crt"
PRIVATE_KEY_FILE = "/private.pem.key"

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
def load_wifi_config():
    with open(WIFI_CONFIG_FILE, "r") as f:
        config = json.load(f)

    ssid = config.get("ssid")
    password = config.get("password")

    if not ssid or password is None:
        raise RuntimeError("Invalid Wi-Fi configuration")

    return ssid, password


def load_aws_credentials():
    with open(ROOT_CA_FILE, "rb") as f:
        root_ca = f.read()

    with open(DEVICE_CERT_FILE, "rb") as f:
        device_cert = f.read()

    with open(PRIVATE_KEY_FILE, "rb") as f:
        private_key = f.read()

    return root_ca, device_cert, private_key

# ==========================================
# 振動設定
# ==========================================
motor_pwm = PWM(Pin(26), freq=1000, duty=0)
HEARTBEAT_POWER = 400

TEMP_CHANNEL = 0

# MQTT受信データを保存する変数
pending_intervals = None


def select_hub_channel(i2c, channel):
    if 0 <= channel <= 5:
        i2c.writeto(0x70, bytes([1 << channel]))
        time.sleep_ms(5)


def vibrate(power, duration_ms):
    motor_pwm.duty(power)
    time.sleep_ms(duration_ms)
    motor_pwm.duty(0)


# ==========================================
# Wi-Fi接続
# ==========================================
def connect_wifi():
    ssid, password = load_wifi_config()

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if not wlan.isconnected():
        wlan.connect(ssid, password)

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
    root_ca, device_cert, private_key = load_aws_credentials()

    tls_params = {
        "key": private_key,
        "cert": device_cert,
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
    for interval in intervals:
        # 1回の振動
        vibrate(HEARTBEAT_POWER, 40)

        # 次の振動までの残り時間
        remaining = max(0, interval - 40)
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
    global pending_intervals

    M5.begin()
    motor_pwm.duty(0)

    M5.Lcd.setTextSize(2)
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

        # MQTT受信を処理
        try:
            mqtt.check_msg()
        except Exception as e:
            print("MQTT receive error:", e)
            motor_pwm.duty(0)
            time.sleep(2)
            # 接続が切れた場合は、再接続が必要です
            mqtt = connect_mqtt()

        # 新しいリズムを受信したら再生
        if pending_intervals is not None:
            intervals = pending_intervals
            pending_intervals = None
            play_rhythm(intervals)

        # 2秒ごとに温度測定
        if time.ticks_diff(time.ticks_ms(), last_temp_time) >= 2000:
            try:
                temperature = read_temperature(i2c0)
                M5.Lcd.setCursor(10, 100)
                M5.Lcd.print(
                    "Temp: " + str(round(temperature, 1)) + " C   "
                )
            except Exception as e:
                print("Temperature error:", e)

            last_temp_time = time.ticks_ms()


if __name__ == "__main__":
    setup()