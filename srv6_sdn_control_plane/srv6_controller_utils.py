#!/usr/bin/python


# General imports
from __future__ import absolute_import, division, print_function
from sshutil.cmd import SSHCommand
import logging
import telnetlib
import socket
import json
import time
import random
from socket import AF_INET
from socket import AF_INET6
# ipaddress dependencies
from ipaddress import IPv4Interface, IPv6Interface
from ipaddress import IPv4Network, IPv6Network
from ipaddress import AddressValueError
# NetworkX dependencies
from networkx.readwrite import json_graph

# Main Routing Table
MAIN_ROUTING_TABLE = 254

ZEBRA_PORT = 2601
SSH_PORT = 22

MIN_TABLE_ID = 2
# Linux kernel supports up to 255 different tables (or 2**32?)
MAX_TABLE_ID = 2**32 - 1  # = 255
# Table where we store our seg6local routes
LOCAL_SID_TABLE = MAIN_ROUTING_TABLE
# Reserved table IDs
RESERVED_TABLEIDS = [0, 253, 254, 255]
RESERVED_TABLEIDS.append(LOCAL_SID_TABLE)

RESERVED_TENANTIDS = [0]

WAIT_TOPOLOGY_INTERVAL = 1


# Logger reference
logger = logging.getLogger(__name__)


# Initialize random seed
random.seed(time.time())


class InterfaceType:
    UNKNOWN = 'unknown'
    WAN = 'wan'
    LAN = 'lan'


'''
class DeviceStatus:
    NOT_CONNECTED = 'Not Connected'
    CONNECTED = 'Connected'
    RUNNING = 'Running'
'''


supported_interface_types = [
    InterfaceType.WAN,
    InterfaceType.LAN
]

# Generate a random token used to authenticate the tenant


def generate_token():
    # Example of token: J4Ie2QKOHz3IVSQs8yA1ahAKfl1ySrtVxGVuT6NkuElGfC8cm55rFhyzkc79pjSLOsr7zKOu7rkMgNMyEHlze4iXVNoX1AtifuieNrrW4rrCroScpGdQqHMETJU46okS  # noqa: E501
    seq = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890'
    token = ''
    for _ in range(0, 128):
        token += random.choice(seq)
    # Return the token
    return token


# Return true if the IP address belongs to the network
def IPv6AddrInNet(ipaddr, net):
    # return IPv6Interface(unicode(ipaddr)) in IPv6Network(unicode(net))
    return IPv6Interface(ipaddr) in IPv6Network(net)


# Find a IPv6 address contained in the net
def findIPv6AddrInNet(ipaddrs, net):
    for ipaddr in ipaddrs:
        if IPv6AddrInNet(ipaddr, net):
            return ipaddr
    return None


def merge_two_dicts(x, y):
    z = x.copy()   # start with x's keys and values
    z.update(y)    # modifies z with y's keys and values & returns None
    return z


def print_and_die(msg, code=-2):
    print(msg)
    exit(code)


class TenantIDAllocator:
    def __init__(self):
        # Set of reusable tenant ID
        self.reusable_tenantids = set()
        # Last used tenant ID
        self.last_allocated_tenantid = -1
        # Mapping token to tenant ID
        self.token_to_tenantid = dict()

    # Allocate and return a new tenant ID for a token
    def get_new_tenantid(self, token):
        if self.token_to_tenantid.get(token):
            # The token already has a tenant ID
            return -1
        else:
            # Check if a reusable tenant ID is available
            if self.reusable_tenantids:
                tenantid = self.reusable_tenantids.pop()
            else:
                # Get new tenant ID
                self.last_allocated_tenantid += 1
                while self.last_allocated_tenantid in RESERVED_TENANTIDS:
                    # Skip reserved tenant ID
                    self.last_allocated_tenantid += 1
                tenantid = self.last_allocated_tenantid

            # If tenant ID is valid
            if validate_tenantid(tenantid) is True:
                # Assigne tenant ID to the token
                self.token_to_tenantid[token] = str(tenantid)
                return str(tenantid)
            # Return -1 if tenant IDs are finished
            else:
                return -1

    # Return tenant ID, if no tenant ID assigned to the token return -1
    def get_tenantid(self, token):
        return str(self.token_to_tenantid.get(token, -1))

    # Release tenant ID and mark it as reusable
    def release_tenantid(self, token):
        # Check if the token has an associated tenantid
        if token in self.token_to_tenantid:
            tenantid = self.token_to_tenantid[token]
            # Unassigne the tenant ID
            del self.token_to_tenantid[token]
            # Mark the tenant ID as reusable
            self.reusable_tenantids.add(tenantid)

            return str(tenantid)
        else:
            # The token has not an associated tenant ID
            return -1

# Table ID Allocator


class TableIDAllocator:

    def __init__(self):
        # Mapping VPN name to table ID, indexed by tenant ID
        self.vpn_to_tableid = dict()
        # Set of reusable table IDs, indexed by tenant ID
        self.reusable_tableids = dict()
        # Last used table ID, indexed by tenant ID
        self.last_allocated_tableid = dict()

    # Allocate and return a new table ID for a VPN
    def get_new_tableid(self, vpn_name, tenantid):
        if tenantid not in self.vpn_to_tableid:
            # Initialize data structures
            self.vpn_to_tableid[tenantid] = dict()
            self.reusable_tableids[tenantid] = set()
            self.last_allocated_tableid[tenantid] = -1
        # Get the new table ID
        if self.vpn_to_tableid[tenantid].get(vpn_name):
            # The VPN already has an associated table ID
            return -1
        else:
            # Check if a reusable table ID is available
            if self.reusable_tableids[tenantid]:
                tableid = self.reusable_tableids[tenantid].pop()
            else:
                # If not, get a new table ID
                self.last_allocated_tableid[tenantid] += 1
                while self.last_allocated_tableid[
                    tenantid
                ] in RESERVED_TABLEIDS:
                    # Skip reserved table IDs
                    self.last_allocated_tableid[tenantid] += 1
                tableid = self.last_allocated_tableid[tenantid]
            # Assign the table ID to the VPN name
            self.vpn_to_tableid[tenantid][vpn_name] = tableid
            # And return
            return tableid

    # Return the table ID assigned to the VPN
    # If the VPN has no assigned table IDs, return -1
    def get_tableid(self, vpn_name, tenantid):
        if tenantid not in self.vpn_to_tableid:
            return -1
        return self.vpn_to_tableid[tenantid].get(vpn_name, -1)

    # Release a table ID and mark it as reusable
    def release_tableid(self, vpn_name, tenantid):
        # Check if the VPN has an associated table ID
        if self.vpn_to_tableid[tenantid].get(vpn_name):
            # The VPN has an associated table ID
            tableid = self.vpn_to_tableid[tenantid][vpn_name]
            # Unassign the table ID
            del self.vpn_to_tableid[tenantid][vpn_name]
            # Mark the table ID as reusable
            self.reusable_tableids[tenantid].add(tableid)
            # If the tenant has no VPNs,
            # destory data structures
            if len(self.vpn_to_tableid[tenantid]) == 0:
                del self.vpn_to_tableid[tenantid]
                del self.reusable_tableids[tenantid]
                del self.last_allocated_tableid[tenantid]
            # Return the table ID
            return tableid
        else:
            # The VPN has not an associated table ID
            return -1


class VTEPIPv6NetAllocator:

    bit = 16
    net = u"fcfb::/%d" % bit
    prefix = 64

    def __init__(self):
        print("*** Calculating Available Mgmt Addresses")
        self.hosts = (IPv6Network(self.net)).hosts()

    def nextVTEPAddress(self):
        n_host = next(self.hosts)
        return n_host.__str__()


class VTEPIPv4NetAllocator:

    bit = 8
    net = u"10.0.0.0/%d" % bit
    prefix = 16

    def __init__(self):
        print("*** Calculating Available Mgmt Addresses")
        self.vtepnet = (IPv4Network(self.net)).hosts()

    def nextVTEPAddress(self):
        n_host = next(self.vtepnet)
        return n_host.__str__()


# Utiliy function to check if the provided table ID is valid
def validate_table_id(tableid):
    return tableid >= MIN_TABLE_ID and tableid <= MAX_TABLE_ID


def validate_deviceid(deviceid):
    return deviceid is not None and deviceid != ''


def validate_overlayid(overlayid):
    return overlayid is not None and overlayid != ''


def validate_tenantid(tenantid):
    return tenantid is not None and tenantid != ''


def validate_overlay_type(overlay_type):
    return overlay_type in supported_overlay_types


def validate_overlay_name(overlay_name):
    return overlay_name is not None and overlay_name != ''


def validate_tunnel_mode(tunnel_mode, supported_tunnel_modes):
    return tunnel_mode in supported_tunnel_modes


def validate_port(port):
    return port >= 0 and port <= 65535


def validate_interface_type(interface_type):
    return interface_type in supported_interface_types


# Utiliy function to check if the IP
# is a valid IPv6 address
def validate_ipv6_address(ip):
    if ip is None:
        return False
    try:
        IPv6Interface(ip)
        return True
    except AddressValueError:
        return False


# Utiliy function to check if the IP
# is a valid IPv4 address
def validate_ipv4_address(ip):
    if ip is None:
        return False
    try:
        IPv4Interface(ip)
        return True
    except AddressValueError:
        return False


# Utiliy function to check if the IP
# is a valid address
def validate_ip_address(ip):
    return validate_ipv4_address(ip) or validate_ipv6_address(ip)


# Utiliy function to get the IP address family
def getAddressFamily(ip):
    if validate_ipv6_address(ip):
        # IPv6 address
        return AF_INET6
    elif validate_ipv4_address(ip):
        # IPv4 address
        return AF_INET
    else:
        # Invalid address
        return None


class OverlayType:
    IPv6Overlay = 'IPv6Overlay'
    IPv4Overlay = 'IPv4Overlay'


supported_overlay_types = [OverlayType.IPv4Overlay, OverlayType.IPv6Overlay]

'''
class VPN:
    # tableid=-1):
    def __init__(self, tunnel_id, vpn_name, vpn_type, interfaces, tenantid,
                 tunnel_mode):
        # Tunnel ID
        self.id = tunnel_id
        # VPN name
        self.vpn_name = vpn_name
        # VPN type
        self.vpn_type = vpn_type
        # Interfaces belonging to the VPN
        self.interfaces = set(interfaces)
        #self.interfaces = dict()
        # for interface in interfaces:
        #    routerid = interface.routerid
        #    interface_name = interface.interface_name
        #    if self.interfaces.get(routerid) is None:
        #        self.interfaces[routerid] = dict()
        #    self.interfaces[routerid][interface_name] = interface
        # Tenant ID
        self.tenantid = tenantid
        # Table ID
        #self.tableid = tableid
        #self.tunnel_specific_data = dict()
        self.tunnel_mode = tunnel_mode

    def removeInterface(self, routerid, interface_name):
        for interface in self.interfaces.copy():
            if interface.routerid == routerid and \
                    interface.interface_name == interface_name:
                self.interfaces.remove(interface)
                return True
        return False

    def numberOfInterfaces(self, routerid):
        num = 0
        for interface in self.interfaces:
            if interface.routerid == routerid:
                num += 1
        return num

    def getInterface(self, routerid, interface_name):
        for interface in self.interfaces:
            if interface.routerid == routerid and \
                    interface.interface_name == interface_name:
                return interface
        return None


class Interface:
    def __init__(self, routerid, interface_name):
        # Router ID
        self.routerid = routerid
        # Interface name
        self.interface_name = interface_name
'''


# IPv6 utility functions

def del_ipv6_nd_prefix_quagga(router, intf, prefix):
    # Establish a telnet connection with the zebra daemon
    # and try to reconfigure the addressing plan of the router
    router = str(router)
    port = ZEBRA_PORT
    intf = str(intf)
    prefix = str(prefix)
    try:
        print('%s - Trying to reconfigure addressing plan' % router)
        password = 'srv6'
        # Init telnet
        tn = telnetlib.Telnet(router, port)
        # Password
        tn.read_until(b'Password: ')
        tn.write(b'%s\r\n' % password.encode())
        # Terminal length set to 0 to not have interruptions
        tn.write(b'terminal length 0\r\n')
        # Enable
        tn.write(b'enable\r\n')
        # Password
        tn.read_until(b'Password: ')
        tn.write(b'%s\r\n' % password.encode())
        # Configure terminal
        tn.write(b'configure terminal\r\n')
        # Interface configuration
        tn.write(b'interface %s\r\n' % intf.encode())
        # Remove old IPv6 prefix
        tn.write(b'no ipv6 nd prefix %s\r\n' % prefix)
        # Close interface configuration
        tn.write(b'q\r\n')
        # Close configuration mode
        tn.write(b'q\r\n')
        # Close privileged mode
        tn.write(b'q\r\n')
        # Close telnet
        tn.close()
    except socket.error:
        print('Error: cannot establish a connection '
              'to %s on port %s' % (str(router), str(port)))


def add_ipv6_nd_prefix_quagga(router, intf, prefix):
    # Establish a telnet connection with the zebra daemon
    # and try to reconfigure the addressing plan of the router
    router = str(router)
    port = ZEBRA_PORT
    intf = str(intf)
    prefix = str(prefix)
    try:
        print('%s - Trying to reconfigure addressing plan' % router)
        password = 'srv6'
        # Init telnet
        tn = telnetlib.Telnet(router, port)
        # Password
        tn.read_until(b'Password: ')
        tn.write(b'%s\r\n' % password.encode())
        # Terminal length set to 0 to not have interruptions
        tn.write(b'terminal length 0\r\n')
        # Enable
        tn.write(b'enable\r\n')
        # Password
        tn.read_until(b'Password: ')
        tn.write(b'%s\r\n' % password.encode())
        # Configure terminal
        tn.write(b'configure terminal\r\n')
        # Interface configuration
        tn.write(b'interface %s\r\n' % intf.encode())
        # Remove old IPv6 prefix
        tn.write(b'ipv6 nd prefix %s\r\n' % prefix.encode())
        # Close interface configuration
        tn.write(b'q\r\n')
        # Close configuration mode
        tn.write(b'q\r\n')
        # Close privileged mode
        tn.write(b'q\r\n')
        # Close telnet
        tn.close()
    except socket.error:
        print('Error: cannot establish a connection '
              'to %s on port %s' % (str(router), str(port)))


def add_ipv6_address_quagga(router, intf, ip):
    # Establish a telnet connection with the zebra daemon
    # and try to reconfigure the addressing plan of the router
    router = str(router)
    port = ZEBRA_PORT
    intf = str(intf)
    ip = str(ip)
    try:
        print('%s - Trying to reconfigure addressing plan' % router)
        password = 'srv6'
        # Init telnet
        tn = telnetlib.Telnet(router, port)
        # Password
        tn.read_until(b'Password: ')
        tn.write(b'%s\r\n' % password.encode())
        # Terminal length set to 0 to not have interruptions
        tn.write(b'terminal length 0\r\n')
        # Enable
        tn.write(b'enable\r\n')
        # Password
        tn.read_until(b'Password: ')
        tn.write(b'%s\r\n' % password.encode())
        # Configure terminal
        tn.write(b'configure terminal\r\n')
        # Interface configuration
        tn.write(b'interface %s\r\n' % intf.encode())
        # Add the new IPv6 address
        tn.write(b'ipv6 address %s\r\n' % ip.encode())
        # Close interface configuration
        tn.write(b'q\r\n')
        # Close configuration mode
        tn.write(b'q\r\n')
        # Close privileged mode
        tn.write(b'q\r\n')
        # Close telnet
        tn.close()
    except socket.error:
        print('Error: cannot establish a connection '
              'to %s on port %s' % (str(router), str(port)))


def del_ipv6_address_quagga(router, intf, ip):
    # Establish a telnet connection with the zebra daemon
    # and try to reconfigure the addressing plan of the router
    router = str(router)
    port = ZEBRA_PORT
    intf = str(intf)
    ip = str(ip)
    try:
        print('%s - Trying to reconfigure addressing plan' % router)
        password = 'srv6'
        # Init telnet
        tn = telnetlib.Telnet(router, port)
        # Password
        tn.read_until(b'Password: ')
        tn.write(b'%s\r\n' % password.encode())
        # Terminal length set to 0 to not have interruptions
        tn.write(b'terminal length 0\r\n')
        # Enable
        tn.write(b'enable\r\n')
        # Password
        tn.read_until(b'Password: ')
        tn.write(b'%s\r\n' % password.encode())
        # Configure terminal
        tn.write(b'configure terminal\r\n')
        # Interface configuration
        tn.write(b'interface %s\r\n' % intf.encode())
        # Add the new IPv6 address
        tn.write(b'no ipv6 address %s\r\n' % ip)
        # Close interface configuration
        tn.write(b'q\r\n')
        # Close configuration mode
        tn.write(b'q\r\n')
        # Close privileged mode
        tn.write(b'q\r\n')
        # Close telnet
        tn.close()
    except socket.error:
        print('Error: cannot establish a connection '
              'to %s on port %s' % (str(router), str(port)))


def flush_ipv6_addresses_ssh(router, intf):
    # Utility to close a ssh session
    def close_ssh_session(session):
        # Close the session
        remoteCmd.close()
        # Flush the cache
        remoteCmd.cache.flush()
    # Flush addresses
    cmd = 'ip -6 addr flush dev %s scope global' % intf
    remoteCmd = SSHCommand(cmd, router, SSH_PORT, 'root', 'root')
    remoteCmd.run_status_stderr()
    # Close the session
    close_ssh_session(remoteCmd)


def add_ipv6_address_ssh(router, intf, ip):
    # Utility to close a ssh session
    def close_ssh_session(session):
        # Close the session
        remoteCmd.close()
        # Flush the cache
        remoteCmd.cache.flush()
    # Add the address
    cmd = 'ip -6 addr add %s dev %s' % (ip, intf)
    remoteCmd = SSHCommand(cmd, router, SSH_PORT, 'root', 'root')
    remoteCmd.run_status_stderr()
    # Close the session
    close_ssh_session(remoteCmd)


def del_ipv6_address_ssh(router, intf, ip):
    # Utility to close a ssh session
    def close_ssh_session(session):
        # Close the session
        remoteCmd.close()
        # Flush the cache
        remoteCmd.cache.flush()
    # Add the address
    cmd = 'ip -6 addr del %s dev %s' % (ip, intf)
    remoteCmd = SSHCommand(cmd, router, SSH_PORT, 'root', 'root')
    remoteCmd.run_status_stderr()
    # Close the session
    close_ssh_session(remoteCmd)


# IPv4 utility functions

def add_ipv4_default_via(router, intf, ip):
    # Utility to close a ssh session
    def close_ssh_session(session):
        # Close the session
        remoteCmd.close()
        # Flush the cache
        remoteCmd.cache.flush()
    # Add the default via
    cmd = 'ip route add default via %s dev %s' % (ip, intf)
    remoteCmd = SSHCommand(cmd, router, SSH_PORT, 'root', 'root')
    remoteCmd.run_status_stderr()
    # Close the session
    close_ssh_session(remoteCmd)


def del_ipv4_default_via(router):
    # Utility to close a ssh session
    def close_ssh_session(session):
        # Close the session
        remoteCmd.close()
        # Flush the cache
        remoteCmd.cache.flush()
    # Add the default via
    cmd = 'ip route del default'
    remoteCmd = SSHCommand(cmd, router, SSH_PORT, 'root', 'root')
    remoteCmd.run_status_stderr()
    # Close the session
    close_ssh_session(remoteCmd)


def add_ipv4_address_quagga(router, intf, ip):
    # Establish a telnet connection with the zebra daemon
    # and try to reconfigure the addressing plan of the router
    router = str(router)
    port = ZEBRA_PORT
    intf = str(intf)
    ip = str(ip)
    try:
        print('%s - Trying to reconfigure addressing plan' % router)
        password = 'srv6'
        # Init telnet
        tn = telnetlib.Telnet(router, port)
        # Password
        tn.read_until(b'Password: ')
        tn.write(b'%s\r\n' % password.encode())
        # Terminal length set to 0 to not have interruptions
        tn.write(b'terminal length 0\r\n')
        # Enable
        tn.write(b'enable\r\n')
        # Password
        tn.read_until(b'Password: ')
        tn.write(b'%s\r\n' % password.encode())
        # Configure terminal
        tn.write(b'configure terminal\r\n')
        # Interface configuration
        tn.write(b'interface %s\r\n' % intf.encode())
        # Add the new IP address
        tn.write(b'ip address %s\r\n' % ip)
        # Close interface configuration
        tn.write(b'q\r\n')
        # Close configuration mode
        tn.write(b'q\r\n')
        # Close privileged mode
        tn.write(b'q\r\n')
        # Close telnet
        tn.close()
    except socket.error:
        print('Error: cannot establish a connection '
              'to %s on port %s' % (str(router), str(port)))


def del_ipv4_address_quagga(router, intf, ip):
    # Establish a telnet connection with the zebra daemon
    # and try to reconfigure the addressing plan of the router
    router = str(router)
    port = ZEBRA_PORT
    intf = str(intf)
    ip = str(ip)
    try:
        print('%s - Trying to reconfigure addressing plan' % router)
        password = 'srv6'
        # Init telnet
        tn = telnetlib.Telnet(router, port)
        # Password
        tn.read_until(b'Password: ')
        tn.write(b'%s\r\n' % password.encode())
        # Terminal length set to 0 to not have interruptions
        tn.write(b'terminal length 0\r\n')
        # Enable
        tn.write(b'enable\r\n')
        # Password
        tn.read_until(b'Password: ')
        tn.write(b'%s\r\n' % password.encode())
        # Configure terminal
        tn.write(b'configure terminal\r\n')
        # Interface configuration
        tn.write(b'interface %s\r\n' % intf.encode())
        # Add the new IP address
        tn.write(b'no ip address %s\r\n' % ip)
        # Close interface configuration
        tn.write(b'q\r\n')
        # Close configuration mode
        tn.write(b'q\r\n')
        # Close privileged mode
        tn.write(b'q\r\n')
        # Close telnet
        tn.close()
    except socket.error:
        print('Error: cannot establish a connection '
              'to %s on port %s' % (str(router), str(port)))


def flush_ipv4_addresses_ssh(router, intf):
    # Utility to close a ssh session
    def close_ssh_session(session):
        # Close the session
        remoteCmd.close()
        # Flush the cache
        remoteCmd.cache.flush()
    # Flush addresses
    cmd = 'ip addr flush dev %s' % intf
    remoteCmd = SSHCommand(cmd, router, SSH_PORT, 'root', 'root')
    remoteCmd.run_status_stderr()
    # Close the session
    close_ssh_session(remoteCmd)


def add_ipv4_address_ssh(router, intf, ip):
    # Utility to close a ssh session
    def close_ssh_session(session):
        # Close the session
        remoteCmd.close()
        # Flush the cache
        remoteCmd.cache.flush()
    # Add the address
    cmd = 'ip addr add %s dev %s' % (ip, intf)
    remoteCmd = SSHCommand(cmd, router, SSH_PORT, 'root', 'root')
    remoteCmd.run_status_stderr()
    # Close the session
    close_ssh_session(remoteCmd)


def del_ipv4_address_ssh(router, intf, ip):
    # Utility to close a ssh session
    def close_ssh_session(session):
        # Close the session
        remoteCmd.close()
        # Flush the cache
        remoteCmd.cache.flush()
    # Add the address
    cmd = 'ip addr del %s dev %s' % (ip, intf)
    remoteCmd = SSHCommand(cmd, router, SSH_PORT, 'root', 'root')
    remoteCmd.run_status_stderr()
    # Close the session
    close_ssh_session(remoteCmd)


# Read topology JSON and return the topology graph
def json_file_to_graph(topo_file):
    # Read JSON file
    with open(topo_file) as f:
        topo_json = json.load(f)
    # Return graph from JSON file
    return json_graph.node_link_graph(topo_json)
