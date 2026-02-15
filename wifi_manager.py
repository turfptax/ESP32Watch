"""
WiFi connection manager for MicroPython.
Handles connecting, reconnecting, and NTP time sync.
"""

import network
import time
import ntptime


class WiFiManager:
    """Manages WiFi connection and NTP time sync."""

    def __init__(self, ssid, password, hostname="esp32-watch"):
        self.ssid = ssid
        self.password = password
        self.hostname = hostname
        self.wlan = network.WLAN(network.STA_IF)

    def connect(self, timeout=15):
        """Connect to WiFi network.

        Args:
            timeout: Max seconds to wait for connection

        Returns:
            True if connected, False if timed out
        """
        if self.wlan.isconnected():
            return True

        self.wlan.active(True)
        self.wlan.config(dhcp_hostname=self.hostname)
        self.wlan.connect(self.ssid, self.password)

        start = time.time()
        while not self.wlan.isconnected():
            if time.time() - start > timeout:
                print(f"WiFi: timeout connecting to {self.ssid}")
                return False
            time.sleep(0.5)

        config = self.wlan.ifconfig()
        print(f"WiFi: connected to {self.ssid}")
        print(f"  IP:      {config[0]}")
        print(f"  Subnet:  {config[1]}")
        print(f"  Gateway: {config[2]}")
        print(f"  DNS:     {config[3]}")
        return True

    def disconnect(self):
        """Disconnect from WiFi."""
        self.wlan.disconnect()
        self.wlan.active(False)

    @property
    def is_connected(self):
        return self.wlan.isconnected()

    @property
    def ip_address(self):
        if self.wlan.isconnected():
            return self.wlan.ifconfig()[0]
        return None

    @property
    def rssi(self):
        """WiFi signal strength in dBm."""
        if self.wlan.isconnected():
            try:
                return self.wlan.status('rssi')
            except Exception:
                # Some MicroPython builds use different API
                return None
        return None

    def sync_ntp(self, server="pool.ntp.org", utc_offset=0):
        """Sync system time from NTP server.

        Args:
            server:     NTP server hostname
            utc_offset: Hours offset from UTC (e.g. -5 for EST)

        Returns:
            True if sync successful
        """
        if not self.wlan.isconnected():
            print("WiFi: not connected, cannot sync NTP")
            return False

        try:
            ntptime.host = server
            ntptime.settime()
            print(f"NTP: time synced from {server}")
            return True
        except Exception as e:
            print(f"NTP: sync failed - {e}")
            return False
