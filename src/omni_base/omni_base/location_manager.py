#!/usr/bin/env python3
"""
Location Manager — runtime user-saved named locations (waypoints).

- Loads the static rooms from config/rooms.yaml (installed).
- Loads/ persists user-defined locations to a writable YAML (default ~/.ros/lumi_saved_locations.yaml).
- Publishes a latched /saved_locations (std_msgs/String JSON) with the merged set.
- Accepts /save_location and /delete_location (std_msgs/String) from web UI or other nodes.
- Voice node subscribes to stay in sync for "go to <name>" commands.
"""
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, HistoryPolicy, DurabilityPolicy
from std_msgs.msg import String
from ament_index_python.packages import get_package_share_directory

import os
import yaml
import json


def _load_static_rooms(path, logger=None):
    """Load canonical rooms (same schema as voice_node: rooms: name: [x, y, yaw])."""
    try:
        with open(path, 'r') as f:
            data = yaml.safe_load(f) or {}
        rooms = data.get('rooms', {}) or {}
        parsed = {str(name): tuple(float(v) for v in coords)
                  for name, coords in rooms.items()}
        if parsed:
            return parsed
        if logger:
            logger.warn(f'rooms file {path} had no rooms; using empty set.')
    except Exception as e:
        if logger:
            logger.warn(f'Could not load rooms file {path}: {e}; using empty set.')
    return {}


def _load_user_locations(path, logger=None):
    """Load user-saved locations from YAML. Returns {} if missing/unreadable."""
    try:
        if os.path.exists(path):
            with open(path, 'r') as f:
                data = yaml.safe_load(f) or {}
            locs = data.get('locations', {}) or {}
            parsed = {str(name): tuple(float(v) for v in coords)
                      for name, coords in locs.items()}
            return parsed
    except Exception as e:
        if logger:
            logger.warn(f'Could not load user locations {path}: {e}')
    return {}


def _save_user_locations(path, locations, logger=None):
    """Write user locations to YAML. Creates parent dir if needed. Returns success."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {'locations': {name: list(coords) for name, coords in locations.items()}}
        with open(path, 'w') as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
        return True
    except Exception as e:
        if logger:
            logger.error(f'Failed to save locations to {path}: {e}')
        return False


class LocationManager(Node):
    def __init__(self):
        super().__init__('location_manager')

        # ── Parameters (preserve prior behavior when not overridden) ──
        default_rooms = os.path.join(
            get_package_share_directory('omni_base'), 'config', 'rooms.yaml')
        self.rooms_file = self.declare_parameter('rooms_file', default_rooms).value

        default_saved = os.path.join(os.path.expanduser('~'), '.ros', 'lumi_saved_locations.yaml')
        self.saved_file = self.declare_parameter('saved_locations_file', default_saved).value

        # ── Load sources ──
        self.static_locations = _load_static_rooms(self.rooms_file, self.get_logger())
        self.user_locations = _load_user_locations(self.saved_file, self.get_logger())

        # ── Latched publisher so late subscribers (web/voice) get current list immediately ──
        latched_qos = QoSProfile(
            depth=1,
            history=HistoryPolicy.KEEP_LAST,
            durability=DurabilityPolicy.TRANSIENT_LOCAL
        )
        self.locations_pub = self.create_publisher(String, '/saved_locations', qos_profile=latched_qos)

        # ── Control topics (string payloads; JSON for save, plain name for delete) ──
        self.create_subscription(String, '/save_location', self._on_save_location, 10)
        self.create_subscription(String, '/delete_location', self._on_delete_location, 10)

        # Publish initial snapshot
        self._publish_current()

        self.get_logger().info(
            f'Location manager ready. '
            f'Static rooms: {len(self.static_locations)}, '
            f'User locations: {len(self.user_locations)}, '
            f'file: {self.saved_file}'
        )

    def _merged(self):
        # User-saved entries override static names on collision (intentional shadowing).
        return {**self.static_locations, **self.user_locations}

    def _publish_current(self):
        payload = json.dumps({'locations': self._merged()})
        self.locations_pub.publish(String(data=payload))

    # ── /save_location formats supported ──
    # 1) JSON: {"name": "abc", "x": 1.23, "y": -0.45, "yaw": 0.0}
    # 2) Pipe-delimited: "abc|1.23|-0.45|0.0"  (yaw optional → defaults to 0)
    def _on_save_location(self, msg: String):
        raw = msg.data.strip()
        if not raw:
            return

        name = None
        x = y = yaw = None
        try:
            if raw.startswith('{'):
                obj = json.loads(raw)
                name = str(obj.get('name', '')).strip()
                x = float(obj['x'])
                y = float(obj['y'])
                yaw = float(obj.get('yaw', 0.0))
            elif '|' in raw:
                parts = [p.strip() for p in raw.split('|')]
                if len(parts) >= 3:
                    name = parts[0]
                    x = float(parts[1])
                    y = float(parts[2])
                    yaw = float(parts[3]) if len(parts) >= 4 else 0.0
            else:
                self.get_logger().warn('save_location: unrecognized format (need JSON or name|x|y|yaw)')
                return
        except Exception as e:
            self.get_logger().warn(f'save_location parse error: {e}')
            return

        if not name:
            self.get_logger().warn('save_location: empty name')
            return

        self.user_locations[name] = (x, y, yaw)
        if _save_user_locations(self.saved_file, self.user_locations, self.get_logger()):
            self.get_logger().info(f'Saved location "{name}" -> ({x:.3f}, {y:.3f}, yaw={yaw:.2f})')
            self._publish_current()
        else:
            # Roll back in-memory on write failure so state stays consistent with disk.
            self.user_locations.pop(name, None)

    def _on_delete_location(self, msg: String):
        name = msg.data.strip()
        if not name:
            return
        if name in self.user_locations:
            del self.user_locations[name]
            if _save_user_locations(self.saved_file, self.user_locations, self.get_logger()):
                self.get_logger().info(f'Deleted saved location "{name}"')
                self._publish_current()
            else:
                self.get_logger().warn(f'Deleted "{name}" from memory but file write failed.')
        else:
            self.get_logger().debug(f'Delete requested for unknown user location "{name}" (ignored)')


def main(args=None):
    rclpy.init(args=args)
    node = LocationManager()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
