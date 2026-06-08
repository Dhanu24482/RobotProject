#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from std_msgs.msg import String, Header
from sensor_msgs.msg import Range, PointCloud2
from sensor_msgs_py import point_cloud2
import serial
import time
import threading


class ArduinoBridge(Node):
    # Ultrasonic sensors in the exact order the firmware streams them:
    #   front-left, front-center, front-right, rear-left, rear-center, rear-right
    US_SENSORS = [
        ('front_left',   'us_front_left'),
        ('front_center', 'us_front_center'),
        ('front_right',  'us_front_right'),
        ('rear_left',    'us_rear_left'),
        ('rear_center',  'us_rear_center'),
        ('rear_right',   'us_rear_right'),
    ]
    # IR pit sensors in firmware order: front, back, left, right.
    # Each entry is (name, (x, y) mount offset in base_link metres).
    IR_SENSORS = [
        ('front', ( 0.32,  0.0)),
        ('back',  (-0.32,  0.0)),
        ('left',  ( 0.0,   0.32)),
        ('right', ( 0.0,  -0.32)),
    ]

    # HC-SR04 characteristics
    US_FOV       = 0.26   # rad (~15 deg)
    US_MIN_RANGE = 0.02   # m
    US_MAX_RANGE = 4.0    # m

    def __init__(self):
        super().__init__('arduino_bridge')

        # Declared ROS params — defaults preserve the previous hardcoded values,
        # so behavior is identical out of the box.
        # Note: min_pwm (deadband) is kept well below max_pwm so that low-speed
        # commands remain usable and the full 0..max_pwm range is reachable.
        self.serial_port      = self.declare_parameter('serial_port', '/dev/arduino').value
        self.baud_rate        = self.declare_parameter('baud_rate', 115200).value
        self.wheel_separation = self.declare_parameter('wheel_separation', 0.35).value
        self.max_speed        = self.declare_parameter('max_speed', 1.0).value
        self.max_pwm          = self.declare_parameter('max_pwm', 30).value
        self.min_pwm          = self.declare_parameter('min_pwm', 20).value
        self.is_driving       = False

        # Serial write lock — traffic is now bidirectional and writes come from
        # both the executor thread (callbacks) and the drive() daemon thread.
        self._tx_lock   = threading.Lock()
        self._rx_buffer = b''

        try:
            self.arduino = serial.Serial(
                self.serial_port, self.baud_rate, timeout=1
            )
            time.sleep(2)
            self.get_logger().info('Arduino connected - NEW BRIDGE V2')
        except Exception as e:
            self.get_logger().error(f'Serial failed: {e}')
            return

        self.create_subscription(
            Twist,  '/cmd_vel',            self.cmd_vel_cb, 10
        )
        self.create_subscription(
            String, '/robot/body/command', self.body_cb,    10
        )

        # ── Sensor publishers (telemetry from the Arduino) ──
        self.us_pubs = {
            name: self.create_publisher(Range, f'/ultrasonic/{name}', 10)
            for name, _frame in self.US_SENSORS
        }
        self.pit_pub = self.create_publisher(PointCloud2, '/pit_obstacles', 10)

        # ── Serial read loop (~20 Hz) — parses telemetry lines ──
        self.create_timer(0.05, self.read_serial)

        self.get_logger().info('Bridge V2 ready.')

    # ── CMD_VEL → motors (Nav2 autonomous navigation) ────────
    def cmd_vel_cb(self, msg):
        l = msg.linear.x  - (msg.angular.z * self.wheel_separation / 2.0)
        r = msg.linear.x  + (msg.angular.z * self.wheel_separation / 2.0)
        lp = int(max(min(self.db((l/self.max_speed)*self.max_pwm), 255), -255))
        rp = int(max(min(self.db((r/self.max_speed)*self.max_pwm), 255), -255))
        self.tx(f'<{lp},{rp}>\n')
        # High-rate stream → debug only, so it doesn't flood the logs.
        self.get_logger().debug(f'Nav2 motors: L={lp} R={rp}')

    # ── BODY COMMAND → servos or motors ──────────────────────
    def body_cb(self, msg):
        # Remove ALL angle brackets first — handles <NOD> and NOD equally
        raw   = msg.data.strip()
        clean = raw.replace('<', '').replace('>', '').strip()
        cmd   = clean.upper()

        self.get_logger().info(f'Received body cmd: "{cmd}"')

        # ── PWM direct motor command: "60,60" or "-20,-20" ───
        if ',' in clean and ':' not in clean:
            parts = clean.split(',')
            if len(parts) == 2:
                try:
                    lp = int(parts[0].strip())
                    rp = int(parts[1].strip())
                    self.tx(f'<{lp},{rp}>\n')
                    self.get_logger().info(f'PWM motors: L={lp} R={rp}')
                    return
                except ValueError:
                    pass

        # ── Text motor shortcuts ──────────────────────────────
        if cmd == 'FORWARD':
            self.drive(self.max_pwm, self.max_pwm, 2.0)
            self.get_logger().info('FORWARD started')

        elif cmd == 'BACKWARD':
            self.drive(-self.max_pwm, -self.max_pwm, 2.0)
            self.get_logger().info('BACKWARD started')

        elif cmd == 'LEFT':
            self.drive(-self.max_pwm, self.max_pwm, 2.5)
            self.get_logger().info('LEFT started')

        elif cmd == 'RIGHT':
            self.drive(self.max_pwm, -self.max_pwm, 2.5)
            self.get_logger().info('RIGHT started')

        elif cmd == 'STOP':
            self.is_driving = False
            self.tx('<0,0>\n')
            self.get_logger().info('STOP sent')

        # ── Servo / animation commands ────────────────────────
        elif cmd in ('NOD','SHAKE','EBLINK','EBLINK2','CENTER','WAVE:L','WAVE:R',
                     'LOOK:L','LOOK:R','LOOK:C') \
          or cmd.startswith(('HP:','EL:','ER:','EY:','HL:','HR:','HANDS:','LOOK:')):
            self.tx(f'<{clean}>\n')
            self.get_logger().info(f'Servo: <{clean}>')

        else:
            self.get_logger().warn(f'BRIDGE V2 - unhandled: "{clean}"')

    # ── Drive for duration then stop ──────────────────────────
    def drive(self, left_pwm, right_pwm, duration):
        if self.is_driving:
            return
        def _go():
            self.is_driving = True
            cmd = f'<{left_pwm},{right_pwm}>\n'
            end = time.time() + duration
            while time.time() < end and self.is_driving:
                self.tx(cmd)
                time.sleep(0.1)   # 10Hz keeps Arduino watchdog happy
            self.tx('<0,0>\n')
            self.is_driving = False
            self.get_logger().info('Drive complete.')
        threading.Thread(target=_go, daemon=True).start()

    # ── Serial read → publish sensor telemetry ────────────────
    def read_serial(self):
        if not (hasattr(self, 'arduino') and self.arduino.is_open):
            return
        try:
            waiting = self.arduino.in_waiting
            if waiting:
                self._rx_buffer += self.arduino.read(waiting)
        except Exception as e:
            self.get_logger().warn(f'Serial read error: {e}')
            return

        # Split complete lines out of the buffer.
        while b'\n' in self._rx_buffer:
            line, self._rx_buffer = self._rx_buffer.split(b'\n', 1)
            text = line.decode('utf-8', errors='ignore').strip()
            if not text:
                continue
            if text.startswith('#'):
                self.parse_telemetry(text)
            else:
                self.get_logger().debug(f'Arduino: {text}')

    def parse_telemetry(self, text):
        # Format: #US:f1,f2,f3,b1,b2,b3;IR:F,B,L,R
        body = text[1:]                       # strip leading '#'
        us_vals, ir_vals = None, None
        for part in body.split(';'):
            if part.startswith('US:'):
                us_vals = part[3:].split(',')
            elif part.startswith('IR:'):
                ir_vals = part[3:].split(',')

        stamp = self.get_clock().now().to_msg()

        if us_vals and len(us_vals) == len(self.US_SENSORS):
            for (name, frame), raw in zip(self.US_SENSORS, us_vals):
                try:
                    cm = int(raw)
                except ValueError:
                    continue
                self.publish_range(name, frame, cm, stamp)

        if ir_vals and len(ir_vals) == len(self.IR_SENSORS):
            self.publish_pits(ir_vals, stamp)

    def publish_range(self, name, frame, cm, stamp):
        msg = Range()
        msg.header = Header(stamp=stamp, frame_id=frame)
        msg.radiation_type = Range.ULTRASOUND
        msg.field_of_view  = self.US_FOV
        msg.min_range      = self.US_MIN_RANGE
        msg.max_range      = self.US_MAX_RANGE
        # 0 cm from firmware = no echo / out of range → report max range (clear).
        msg.range = self.US_MAX_RANGE if cm <= 0 else cm / 100.0
        self.us_pubs[name].publish(msg)

    def publish_pits(self, ir_vals, stamp):
        # Each IR that reports a pit becomes a virtual obstacle point at its
        # mount location in base_link, so the Nav2 costmap routes around it.
        points = []
        for (name, (x, y)), raw in zip(self.IR_SENSORS, ir_vals):
            if raw.strip() == '1':
                points.append((float(x), float(y), 0.05))
        header = Header(stamp=stamp, frame_id='base_link')
        cloud = point_cloud2.create_cloud_xyz32(header, points)
        self.pit_pub.publish(cloud)

    # ── Helpers ───────────────────────────────────────────────
    def db(self, pwm):
        if   pwm > 0 and pwm <  self.min_pwm: return  self.min_pwm
        elif pwm < 0 and pwm > -self.min_pwm: return -self.min_pwm
        return pwm

    def tx(self, cmd):
        if hasattr(self, 'arduino') and self.arduino.is_open:
            with self._tx_lock:
                self.arduino.write(cmd.encode('utf-8'))


def main(args=None):
    rclpy.init(args=args)
    node = ArduinoBridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if hasattr(node, 'arduino') and node.arduino.is_open:
            node.tx('<0,0>\n')
            node.arduino.close()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
