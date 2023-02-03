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

from __future__ import absolute_import, division, print_function

# General imports
import logging

# Reserved GRE keys
RESERVED_GRE_KEYS = []

# ################## Setup these variables ##################

# Path of the proto files
# PROTO_FOLDER = "/home/user/repos/srv6-sdn-proto/"

# ###########################################################


# Adjust relative paths
# script_path = os.path.dirname(os.path.abspath(__file__))
# PROTO_FOLDER = os.path.join(script_path, PROTO_FOLDER)

# Check paths
# if PROTO_FOLDER == '':
#    print('Error: Set PROTO_FOLDER variable in nb_grpc_client.py')
#    sys.exit(-2)
# if not os.path.exists(PROTO_FOLDER):
#    print('Error: PROTO_FOLDER variable in nb_grpc_client.py '
#          'points to a non existing folder\n')
#    sys.exit(-2)

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


# Logger reference
logger = logging.getLogger(__name__)


# GRE key Allocator
class GREKeyAllocator:
    def __init__(self):
        # Mapping VPN name to GRE key
        self.vpn_to_key = dict()
        # Mapping GRE key to tenant ID
        self.key_to_tenantid = dict()
        # Set of reusable GRE keys
        self.reusable_keys = set()
        # Last used GRE key
        self.last_allocated_key = -1

    # Allocate and return a new GRE key for a VPN
    def get_new_gre_key(self, vpn_name, tenantid, local_router, remote_router):
        if self.vpn_to_key.get((vpn_name, local_router, remote_router)):
            # The VPN already has an associated GRE key
            return -1
        else:
            # Check if a reusable GRE key is available
            if self.reusable_keys:
                key = self.reusable_keys.pop()
            else:
                # If not, get a new GRE key
                self.last_allocated_key += 1
                while self.last_allocated_key in RESERVED_GRE_KEYS:
                    # Skip reserved GRE keys
                    self.last_allocated_key += 1
                key = self.last_allocated_key
            # Assign the GRE key to the VPN name
            self.vpn_to_key[(vpn_name, local_router, remote_router)] = key
            # Associate the GRE key to the tenant ID
            self.key_to_tenantid[key] = tenantid
            # And return
            return key

    # Return the GRE key assigned to the VPN
    # If the VPN has no assigned GRE keys, return -1
    def get_gre_key(self, vpn_name, local_router, remote_router):
        return self.vpn_to_key.get((vpn_name, local_router, remote_router), -1)

    # Release a GRE key and mark it as reusable
    def release_gre_key(self, vpn_name, local_router, remote_router):
        # Check if the VPN has an associated GRE key
        if self.vpn_to_key.get((vpn_name, local_router, remote_router)):
            # The VPN has an associated GRE key
            key = self.vpn_to_key[(vpn_name, local_router, remote_router)]
            # Unassign the GRE key
            del self.vpn_to_key[(vpn_name, local_router, remote_router)]
            # Delete the association GRE key - tenant ID
            del self.key_to_tenantid[key]
            # Mark the GRE key as reusable
            self.reusable_keys.add(key)
            # Return the GRE key
            return key
        else:
            # The VPN has not an associated GRE key
            return -1


class ControllerStateGRE:
    """This class maintains the state of the SRv6 controller and provides some
       methods to handle it
    """

    def __init__(self, controller_state):
        # Create Table IDs allocator
        # self.tableid_allocator = controller_state.tableid_allocator
        self.tableid_allocator = None
        # Create GRE keys allocator
        self.gre_key_allocator = GREKeyAllocator()
        # Controller state
        self.controller_state = controller_state
        # Interfaces in VPN
        self.interfaces_in_vpn = dict()
        # Overlay types
        self.overlay_type = dict()
        # VRF interfaces
        self.vrf_interfaces = dict()

    # Get a new table ID
    def get_new_tableid(self, vpn_name, tenantid):
        return self.tableid_allocator.get_new_tableid(vpn_name, tenantid)

    # Get a new table ID
    def get_tableid(self, vpn_name):
        return self.tableid_allocator.get_tableid(vpn_name)

    # Release a table ID
    def release_tableid(self, vpn_name):
        return self.tableid_allocator.release_tableid(vpn_name)

    # Get a new GRE key
    def get_new_gre_key(self, vpn_name, tenantid, local_router, remote_router):
        return self.gre_key_allocator.get_new_gre_key(
            vpn_name, tenantid, local_router, remote_router
        )

    # Get a new table ID
    def get_gre_key(self, vpn_name, local_router, remote_router):
        return self.gre_key_allocator.get_gre_key(
            vpn_name, local_router, remote_router
        )
