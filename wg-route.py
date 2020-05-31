#!/usr/bin/python3

# Copyright (C) 2020 Spearline, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import subprocess
import socket
import threading
import socketserver
import time
import sys

# TODO: Read from config file
wg_backbone_iface = "backbone"
wg_client_ifaces = ["clients"]

class TCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        # self.request is the TCP socket connected to the client
        try:
            self.data = self.request.recv(1024).strip()
            self.data = self.data.decode('utf-8').split(',')
        except:
            print("Error in data from {}: {}".format(self.client_address[0], self.data))
            return
        if self.data[0] == "refresh":
            wgstatus.send_routes_to_host(self.client_address[0] + "/32")
        elif self.data[0] == "update":
            wgstatus.queue_route(self.data[1], int(self.data[2]), self.client_address[0])
        else:
            print("Unknown command from {}: {}".format(self.client_address[0], self.data[0]))


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True

class WGStatus(object):
    def __init__(self):
        self.commands = []
        self.wg_servers = {}

    def run_cmd(self, cmd, count=0):
        try:
            return subprocess.run(cmd, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=30)
        except subprocess.TimeoutExpired as e:
            if count > 5:
                print("Unable to run '{}'".format(cmd))
                sys.exit(1)
            return run_cmd(cmd, count+1)

    def add_host_to_wg(self, key, ip, host, allowed_ips):
        if host != "127.0.0.1":
            ret = self.run_cmd(("wg", "set", wg_backbone_iface, "peer", key, "allowed-ips", allowed_ips.replace(" ", ",") + "," + ip + "/32"))
        try:
            ret = self.run_cmd(("ip", "route", "del", ip + "/32"))
        except subprocess.CalledProcessError:
            pass
        if host != "127.0.0.1":
            ret = self.run_cmd(("ip", "route", "add", ip + "/32", "via", host, "dev", wg_backbone_iface))

    def broadcast(self, dest_ip, ip, age):
        dest_ip = dest_ip.split(',')[0].split('/')
        try:
            if dest_ip[1] != "32":
                print("Invalid destination IP: {}/{} - Not a /32 netmask".format(dest_ip[0], dest_ip[1]))
                return
        except IndexError:
            return
        process = threading.Thread(target=client, args=(dest_ip[0], port, "update,{},{}".format(ip, age)))
        process.start()

    def get_peers(self):
        ret = self.run_cmd(("wg", "show", wg_backbone_iface, "dump"))
        found=False
        for item in ret.stdout.split("\n"):
            try:
                item = item.split("\t")
                key = item[0]
                bb_host = item[2]
                allowed_ips = item[3]
                last_seen = item[4]
                (bb_ip, bb_port) = bb_host.split(":")
                yield key, bb_ip, bb_port, allowed_ips, last_seen
            except (ValueError, IndexError):
                continue

    def update_route(self, ip, host, age, broadcast=False):
        for item in self.get_peers():
            key, bb_ip, bb_port, allowed_ips, last_seen = item
            if broadcast:
                self.broadcast(allowed_ips, ip, age)

            if host in allowed_ips:
                self.add_host_to_wg(key, ip, host, allowed_ips)
                found=True
                break

        if host == "127.0.0.1":
            self.add_host_to_wg("", ip, host, "")
            return

        if not found:
            print("Unable to find host {} in backbone list".format(host))

    def read_route(self, ip, age, host, broadcast=False):
        if age == 0:
            return

        if ip not in self.wg_servers or \
           'age' not in self.wg_servers[ip] or \
           'host' not in self.wg_servers[ip] or \
           age > self.wg_servers[ip]['age']:
            try:
                old_host = self.wg_servers[ip]['host']
            except KeyError:
                old_host = None
            self.wg_servers[ip] = {'age': age, 'host': host}
            if host != old_host:
                print('Setting {} as upstream for {}'.format(host, ip))
                self.update_route(ip, host, age, broadcast)

    def queue_route(self, ip, age, host):
        self.commands.append(('add_route', (ip, age, host)))

    def send_routes_to_host(self, host):
        for ip in self.wg_servers:
            if self.wg_servers[ip]["host"] != "127.0.0.1":
                continue
            self.broadcast(host, ip, self.wg_servers[ip]["age"])

    def send_routes(self):
        try:
            for item in self.get_peers():
                key, bb_ip, bb_port, allowed_ips, last_seen = item
                self.send_routes_to_host(allowed_ips)
        except subprocess.CalledProcessError:
            print("Error sending routes")

    def send_refresh(self, dest_ip):
        dest_ip = dest_ip.split(',')[0].split('/')
        try:
            if dest_ip[1] != "32":
                print("Invalid destination IP: {}/{} - Not a /32 netmask".format(dest_ip[0], dest_ip[1]))
                return
        except IndexError:
            return
        print("Requesting refresh from {}".format(dest_ip[0]))
        process = threading.Thread(target=client, args=(dest_ip[0], port, "refresh"))
        process.start()

    def refresh(self):
        try:
            for item in self.get_peers():
                key, bb_ip, bb_port, allowed_ips, last_seen = item
                self.send_refresh(allowed_ips)
        except subprocess.CalledProcessError:
            print("Error refreshing peers")

    def local_loop(self):
        count = 0
        while True:
            count += 1
            while len(self.commands) > 0:
                command = self.commands.pop(0)
                if command[0] == "add_route":
                    self.read_route(command[1][0], command[1][1], command[1][2])

            if count % 5 == 0:
                for iface in wg_client_ifaces:
                    try:
                        ret = self.run_cmd(("wg", "show", iface, "dump"))
                    except subprocess.CalledProcessError:
                        continue
                    for item in ret.stdout.split("\n"):
                        try:
                            item = item.split("\t")
                            key = item[0]
                            dest_host = item[3]
                            age = int(item[4])
                            (ip, mask) = dest_host.split("/")
                            if mask != "32":
                                continue
                        except (ValueError, IndexError):
                            continue
                        self.read_route(ip, age, '127.0.0.1', True)

            if count > 60:
                count = 0
                self.send_routes()
            time.sleep(1)


def client(ip, port, message):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(45)
            sock.connect((ip, port))
            sock.sendall(bytes(message, 'ascii'))
            response = str(sock.recv(1024), 'ascii')
    except:
        print("Timeout connecting to {}".format(ip))

if __name__ == "__main__":
    HOST, PORT = "0.0.0.0", 3912

    wgstatus = WGStatus()
    server = ThreadedTCPServer((HOST, PORT), TCPHandler)
    with server:
        ip, port = server.server_address
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.daemon = True
        server_thread.start()
        wgstatus.refresh()
        wgstatus.local_loop()
        server.shutdown()
