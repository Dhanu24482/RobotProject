import os
import signal
import sys
import threading

from .server import serve


def main():
    host = os.environ.get('LUMI_PANEL_HOST')      # default: decided by the listen_lan setting
    port = int(os.environ.get('LUMI_PANEL_PORT', '8090'))
    ctx, httpd = serve(host, port)

    def shutdown(*_):
        # Stop the robot stack cleanly (motors get <0,0>) before the service exits.
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        httpd.serve_forever()
    finally:
        ctx.sup.shutdown()
        sys.exit(0)


if __name__ == '__main__':
    main()
