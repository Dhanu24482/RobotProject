#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from std_msgs.msg import String
import serial
import time
import threading

class ArduinoBridge(Node):
    def __init__(self):
        super().__init__('arduino_bridge')

        self.serial_port      = '/dev/arduino'
        self.baud_rate        = 115200
        self.wheel_separation = 0.35
        self.max_speed        = 1.0
        self.max_pwm          = 60
        self.min_pwm          = 45
        self.is_driving       = False

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
        self.get_logger().info('Bridge V2 ready.')

    # ── CMD_VEL → motors (Nav2 autonomous navigation) ────────
    def cmd_vel_cb(self, msg):
        l = msg.linear.x  - (msg.angular.z * self.wheel_separation / 2.0)
        r = msg.linear.x  + (msg.angular.z * self.wheel_separation / 2.0)
        lp = int(max(min(self.db((l/self.max_speed)*self.max_pwm), 255), -255))
        rp = int(max(min(self.db((r/self.max_speed)*self.max_pwm), 255), -255))
        self.tx(f'<{lp},{rp}>\n')
        self.get_logger().info(f'Nav2 motors: L={lp} R={rp}')

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
        elif cmd in ('NOD','SHAKE','EBLINK','CENTER','WAVE:L','WAVE:R') \
          or cmd.startswith(('HP:','EL:','ER:','EY:','HL:','HR:','HANDS:')):
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

    # ── Helpers ───────────────────────────────────────────────
    def db(self, pwm):
        if   pwm > 0 and pwm <  self.min_pwm: return  self.min_pwm
        elif pwm < 0 and pwm > -self.min_pwm: return -self.min_pwm
        return pwm

    def tx(self, cmd):
        if hasattr(self, 'arduino') and self.arduino.is_open:
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
