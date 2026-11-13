"""Test-only egress guard: offline server may use LAN sockets, never cloud APIs."""
import ipaddress
import os
import socket
import sys

if os.getenv("DG_OFFLINE_GUARD") == "1":
    def local(host):
        if host in {"localhost", socket.gethostname(), None, ""}:
            return True
        try:
            return not ipaddress.ip_address(host.split("%")[0]).is_global
        except ValueError:
            return False

    def audit(event, args):
        host = None
        if event == "socket.getaddrinfo":
            host = args[0]
        elif event in {"socket.connect", "socket.sendto"}:
            address = args[-1]
            if isinstance(address, tuple):
                host = address[0]
        if host is not None and not local(host):
            raise RuntimeError(f"Offline external network access denied: {host}")
    sys.addaudithook(audit)
    sys.stderr.write("Offline external-network guard enabled\n")
