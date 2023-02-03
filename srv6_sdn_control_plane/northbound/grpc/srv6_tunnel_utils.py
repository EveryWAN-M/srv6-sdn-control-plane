#!/usr/bin/python

# Copyright (C) 2018 Carmine Scarpitta, Pier Luigi Ventre, Stefano Salsano -
# (CNIT and University of Rome "Tor Vergata")
#
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Server of a Northbound interface based on gRPC protocol
#
# @author Carmine Scarpitta <carmine.scarpitta.94@gmail.com>
# @author Pier Luigi Ventre <pier.luigi.ventre@uniroma2.it>
# @author Stefano Salsano <stefano.salsano@uniroma2.it>
#

# General imports
from __future__ import absolute_import, division, print_function
import logging
# ipaddress dependencies
from ipaddress import IPv6Interface
from ipaddress import IPv6Network
# SRv6 dependencies
from srv6_sdn_controller_state import (
    srv6_sdn_controller_state as storage_helper
)

ZEBRA_PORT = 2601
SSH_PORT = 22
MIN_TABLE_ID = 2
# Linux kernel supports up to 255 different tables
MAX_TABLE_ID = 255
# Table where we store our seg6local routes
LOCAL_SID_TABLE = 1
# Reserved table IDs
RESERVED_TABLEIDS = [0, 253, 254, 255]
RESERVED_TABLEIDS.append(LOCAL_SID_TABLE)

WAIT_TOPOLOGY_INTERVAL = 1

DEFAULT_SID_PREFIX = 'fc00::/64'

# Logger reference
logger = logging.getLogger(__name__)


class SIDAllocatorLoopback(object):

    # Address space for SIDs: fcff:xxxx:2:0/64
    # (e.g. fcff:xxxx:0002:0000:0000:0002:0000:tttt)
    # where 'xxxx' is the router id and tttt the vpn id

    prefix = 64

    def getSID(self, loopbackip, vpn_id):
        # Generate the SID
        prefix = int(IPv6Interface(loopbackip).ip)
        sid = IPv6Network(prefix | 2 << 80 | vpn_id)
        # Remove /128 mask and convert to string
        sid = IPv6Interface(sid).ip.__str__()
        # Return the SID
        return sid

    def getSIDFamily(self, loopbackip):
        # Generate the SID
        prefix = int(IPv6Interface(loopbackip).ip)
        sidFamily = IPv6Network(prefix | 2 << 80)
        # Append prefix /64
        sidFamily = sidFamily.supernet(new_prefix=SIDAllocator.prefix)
        # Convert to string
        sidFamily = IPv6Interface(sidFamily).__str__()
        # Return the SID
        return sidFamily


class SIDAllocator(object):

    # Address space for SIDs: fcff:xxxx:2:0/64
    # (e.g. fcff:xxxx:0002:0000:0000:0002:0000:tttt)
    # where 'xxxx' is the router id and tttt the vpn id

    prefix = 64

    def getSID(self, sid_prefix, vpn_id):
        # Generate the SID
        prefix = int(IPv6Interface(sid_prefix).ip)
        sid = IPv6Network(prefix | vpn_id)
        # Remove /128 mask and convert to string
        sid = IPv6Interface(sid).ip.__str__()
        # Return the SID
        return sid

    def getSIDFamily(self, sid_prefix):
        # Generate the SID
        prefix = int(IPv6Interface(sid_prefix).ip)
        sidFamily = IPv6Network(prefix)
        # Append prefix /64
        sidFamily = sidFamily.supernet(new_prefix=SIDAllocator.prefix)
        # Convert to string
        sidFamily = IPv6Interface(sidFamily).__str__()
        # Return the SID
        return sidFamily


class ControllerStateSRv6Loopback:
    """This class maintains the state of the SRv6 controller and provides some
       methods to handle it
    """

    def __init__(self, controller_state):
        # Create SIDs allocator
        self.sid_allocator = SIDAllocatorLoopback()

    # Return SID
    def get_sid(self, deviceid, tenantid, tableid):
        loopbacknet = storage_helper.get_loopbacknet_ipv6(
            deviceid, tenantid
        )
        return self.sid_allocator.getSID(loopbacknet, tableid)

    # Return SID
    def get_sid_family(self, deviceid, tenantid):
        loopbacknet = storage_helper.get_loopbacknet_ipv6(
            deviceid, tenantid
        )
        return self.sid_allocator.getSIDFamily(loopbacknet)


class ControllerStateSRv6:
    """This class maintains the state of the SRv6 controller and provides some
       methods to handle it
    """

    def __init__(self, controller_state):
        # Create SIDs allocator
        self.sid_allocator = SIDAllocator()

    # Return SID
    def get_sid(self, deviceid, tenantid, tableid):
        # Get the public prefix length
        public_prefix_length = storage_helper.get_public_prefix_length(
            deviceid, tenantid
        )
        if public_prefix_length is None or public_prefix_length == 128:
            # The device is reachable only on the IPv6 address
            # We have not a public subnet from which we can allocate SIDs, so
            # we are forced to use two SIDs:
            # - Public IPv6 address
            # - Decap SID (e.g. End.DT4 or End.DT6) allocated from the private
            #   range
            sid_prefix = storage_helper.get_sid_prefix(
                deviceid, tenantid
            )
            if sid_prefix is None:
                sid_prefix = DEFAULT_SID_PREFIX
        else:  # public_prefix_length < 128
            # In this case we have a subnet from which we can allocate SIDs
            wan_interface = storage_helper.get_wan_interfaces(
                deviceid, tenantid
            )[0]
            ipv6_addrs = storage_helper.get_global_ipv6_addresses(
                deviceid, tenantid, wan_interface
            )
            if ipv6_addrs is None or len(ipv6_addrs) == 0:
                ipv6_addrs = storage_helper.get_non_link_local_ipv6_addresses(
                    deviceid=deviceid,
                    tenantid=tenantid,
                    interface_name=wan_interface
                )
            ipv6_addr = ipv6_addrs[0].split('/')[0]
            sid_prefix = str(
                IPv6Network(ipv6_addr).supernet(
                    new_prefix=public_prefix_length
                )
            )
        # Generate local SID from SID prefix
        return self.sid_allocator.getSID(sid_prefix, tableid)

    # Return SID
    def get_sid_family(self, deviceid, tenantid):
        # Get the public prefix length
        public_prefix_length = storage_helper.get_public_prefix_length(
            deviceid, tenantid
        )
        if public_prefix_length is None or public_prefix_length == 128:
            # The device is reachable only on the IPv6 address
            # We have not a public subnet from which we can allocate SIDs, so
            # we are forced to use two SIDs:
            # - Public IPv6 address
            # - Decap SID (e.g. End.DT4 or End.DT6) allocated from the private
            #   range
            sid_prefix = storage_helper.get_sid_prefix(
                deviceid, tenantid
            )
            if sid_prefix is None:
                sid_prefix = DEFAULT_SID_PREFIX
        else:  # public_prefix_length < 128
            # In this case we have a subnet from which we can allocate SIDs
            wan_interface = storage_helper.get_wan_interfaces(
                deviceid, tenantid)[0]
            ipv6_addrs = storage_helper.get_global_ipv6_addresses(
                deviceid, tenantid, wan_interface
            )
            if ipv6_addrs is None or len(ipv6_addrs) == 0:
                ipv6_addrs = storage_helper.get_non_link_local_ipv6_addresses(
                    deviceid=deviceid,
                    tenantid=tenantid,
                    interface_name=wan_interface
                )
            ipv6_addr = ipv6_addrs[0].split('/')[0]
            sid_prefix = str(
                IPv6Network(ipv6_addr).supernet(
                    new_prefix=public_prefix_length
                )
            )
        # Generate local SID family from SID prefix
        return self.sid_allocator.getSIDFamily(sid_prefix)

    def get_sid_list(self, deviceid, tenantid, tableid):
        # Get the public prefix length
        public_prefix_length = storage_helper.get_public_prefix_length(
            deviceid, tenantid
        )
        if public_prefix_length is None or public_prefix_length == 128:
            # The device is reachable only on the IPv6 address
            # We have not a public subnet from which we can allocate SIDs, so
            # we are forced to use two SIDs:
            # - Public IPv6 address
            # - Decap SID (e.g. End.DT4 or End.DT6) allocated from the private
            #   range
            wan_interface = storage_helper.get_wan_interfaces(
                deviceid, tenantid
            )[0]
            ipv6_addrs = storage_helper.get_global_ipv6_addresses(
                deviceid, tenantid, wan_interface
            )
            if ipv6_addrs is None or len(ipv6_addrs) == 0:
                ipv6_addrs = storage_helper.get_non_link_local_ipv6_addresses(
                    deviceid=deviceid,
                    tenantid=tenantid,
                    interface_name=wan_interface
                )
            ipv6_addr = ipv6_addrs[0].split('/')[0]
            sid_list = [ipv6_addr, self.get_sid(deviceid, tenantid, tableid)]
        else:  # public_prefix_length < 128
            # In this case we have a subnet from which we can allocate SIDs
            sid_list = [self.get_sid(deviceid, tenantid, tableid)]
        # Return the SID list
        return sid_list
