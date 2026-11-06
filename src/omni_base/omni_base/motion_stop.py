"""Shared "make the robot stop NOW" routine used by voice_node and remote_node.

A single `<0,0>` is not enough to stop Lumi:
  * Nav2 keeps publishing /cmd_vel (~20 Hz) and the bridge writes every message to the
    motors, so a lone stop is overwritten within milliseconds while a goal is active;
  * the bridge's timed FORWARD/BACKWARD/LEFT/RIGHT pulses keep re-sending until told to stop.

halt() therefore (1) sends the bridge STOP (ends timed pulses + writes <0,0>), (2) cancels
every active Nav2 NavigateToPose goal, (3) publishes zero velocity on /cmd_vel, and
(4) repeats 1+3 shortly afterwards to beat any command already in flight (velocity
smoother output, a serial write that was mid-flight).
"""
import threading

from action_msgs.srv import CancelGoal
from geometry_msgs.msg import Twist
from std_msgs.msg import String

# Re-assert the stop at these delays (seconds) after the first one.
REPEAT_DELAYS = (0.3, 0.8)


class Halter:
    def __init__(self, node, body_topic='/robot/body/command', cmd_vel_topic='/cmd_vel',
                 nav_cancel_service='/navigate_to_pose/_action/cancel_goal'):
        self.node = node
        self.body_pub = node.create_publisher(String, body_topic, 10)
        self.vel_pub = node.create_publisher(Twist, cmd_vel_topic, 10)
        self.cancel_client = node.create_client(CancelGoal, nav_cancel_service)

    def _pulse(self, first=False):
        # Only the first pulse is the bridge STOP (it also starts the bridge's brief
        # /cmd_vel hold-off and cancels timed drives). Repeats are plain <0,0> so a new
        # command issued right after "stop" is not cancelled by a late repeat.
        try:
            msg = String(); msg.data = 'STOP' if first else '<0,0>'
            self.body_pub.publish(msg)
            self.vel_pub.publish(Twist())          # all zeros
        except Exception:
            pass                                   # context may be shutting down

    def _cancel_nav(self):
        try:
            if self.cancel_client.service_is_ready():
                # Empty goal_info (zero UUID + zero stamp) = cancel ALL goals.
                self.cancel_client.call_async(CancelGoal.Request())
        except Exception as e:
            self.node.get_logger().warn(f'Could not cancel Nav2 goal: {e}')

    def halt(self):
        """Stop everything. Safe to call from any thread, any number of times."""
        self._cancel_nav()
        self._pulse(first=True)
        for delay in REPEAT_DELAYS:
            t = threading.Timer(delay, self._pulse)
            t.daemon = True
            t.start()
        # The cancel can race a goal that is still being accepted; try once more.
        t = threading.Timer(REPEAT_DELAYS[0], self._cancel_nav)
        t.daemon = True
        t.start()
