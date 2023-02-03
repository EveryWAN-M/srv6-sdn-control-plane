#!/usr/bin/python
from ipaddress import IPv4Interface, IPv6Interface
from ipaddress import AddressValueError
from socket import AF_INET, AF_INET6


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
