#!/usr/bin/python

# Copyright (C) 2018 Carmine Scarpitta, Pier Luigi Ventre, Stefano Salsano -
# (CNIT and University of Rome "Tor Vergata")
#
#
# Licensed under the Apache License, Version 2.0 (the "License");
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
from six import text_type
from argparse import ArgumentParser
from concurrent import futures
import logging
import time
import grpc
import os
import sys
from rollbackcontext import RollbackContext
from socket import AF_UNSPEC
from socket import AF_INET
from socket import AF_INET6
# ipaddress dependencies
from ipaddress import IPv6Interface, IPv6Network, IPv4Network
# SRv6 dependencies
from srv6_sdn_proto import srv6_vpn_pb2_grpc
from srv6_sdn_proto import srv6_vpn_pb2
from srv6_sdn_control_plane import srv6_controller_utils
from srv6_sdn_control_plane.northbound.grpc import tunnel_utils
from srv6_sdn_control_plane.southbound.grpc import sb_grpc_client
from srv6_sdn_proto import status_codes_pb2
from srv6_sdn_controller_state import (
    srv6_sdn_controller_state as storage_helper
)
from srv6_sdn_proto.status_codes_pb2 import Status, NbStatusCode, SbStatusCode
from srv6_sdn_control_plane.srv6_controller_utils import (
    OverlayType,
    InterfaceType
)
from srv6_sdn_proto.srv6_vpn_pb2 import TenantReply, OverlayServiceReply
from srv6_sdn_proto.srv6_vpn_pb2 import InventoryServiceReply
from srv6_sdn_proto.srv6_vpn_pb2 import GetSIDListsReply

# STAMP Support
ENABLE_STAMP_SUPPORT = True

# Import modules required by STAMP
if ENABLE_STAMP_SUPPORT:
    from srv6_delay_measurement import controller as stamp_controller_module
    from srv6_delay_measurement.exceptions import (
        NodeIdNotFoundError,
        STAMPSessionsExistError
    )

# Topology file
DEFAULT_TOPOLOGY_FILE = '/tmp/topology.json'
# VPN file
DEFAULT_VPN_DUMP = '/tmp/vpn.json'
# Use management IPs instead of loopback IPs
DEFAULT_USE_MGMT_IP = False


# Global variables definition

# Default server ip and port
DEFAULT_GRPC_SERVER_IP = '::'
DEFAULT_GRPC_SERVER_PORT = 54321
DEFAULT_GRPC_CLIENT_PORT = 12345
# Secure option
DEFAULT_SECURE = False
# Server certificate
DEFAULT_CERTIFICATE = 'cert_server.pem'
# Server key
DEFAULT_KEY = 'key_server.pem'
# Southbound interface
DEFAULT_SB_INTERFACE = 'gRPC'
# Verbose mode
DEFAULT_VERBOSE = False
# Seconds between checks for interfaces.json
# and topology.json files
INTERVAL_CHECK_FILES = 5
# Supported southbound interfaces
SUPPORTED_SB_INTERFACES = ['gRPC']
# Validate topology
VALIDATE_TOPO = False
# Default VXLAN port
DEFAULT_VXLAN_PORT = 4789

# Status codes
STATUS_OK = NbStatusCode.STATUS_OK
STATUS_BAD_REQUEST = NbStatusCode.STATUS_BAD_REQUEST
STATUS_INTERNAL_SERVER_ERROR = NbStatusCode.STATUS_INTERNAL_SERVER_ERROR


def exec_or_mark_device_inconsitent(rollback_func, deviceid, tenantid, *args,
                                    **kwargs):
    try:
        if rollback_func(*args, **kwargs) != SbStatusCode.STATUS_SUCCESS:
            # Change device state to reboot required
            success = storage_helper.change_device_state(
                deviceid=deviceid,
                tenantid=tenantid,
                new_state=storage_helper.DeviceState.REBOOT_REQUIRED
            )
            if success is False or success is None:
                logging.error('Error changing the device state')
                return status_codes_pb2.STATUS_INTERNAL_ERROR
    except Exception:
        # Change device state to reboot required
        success = storage_helper.change_device_state(
            deviceid=deviceid,
            tenantid=tenantid,
            new_state=storage_helper.DeviceState.REBOOT_REQUIRED
        )
        if success is False or success is None:
            logging.error('Error changing the device state')
            return status_codes_pb2.STATUS_INTERNAL_ERROR


class NorthboundInterface(srv6_vpn_pb2_grpc.NorthboundInterfaceServicer):
    """gRPC request handler"""

    def __init__(self, grpc_client_port=DEFAULT_GRPC_CLIENT_PORT,
                 srv6_manager=None,
                 southbound_interface=DEFAULT_SB_INTERFACE,
                 verbose=DEFAULT_VERBOSE,
                 stamp_controller=None):
        # Port of the gRPC client
        self.grpc_client_port = grpc_client_port
        # Verbose mode
        self.verbose = verbose
        # Southbound interface
        self.southbound_interface = southbound_interface
        # SRv6 Manager
        self.srv6_manager = srv6_manager
        # Store the reference to the STAMP controller
        self.stamp_controller = stamp_controller
        # Initialize tunnel state
        self.tunnel_modes = tunnel_utils.TunnelState(
            grpc_client_port, verbose
        ).tunnel_modes
        self.supported_tunnel_modes = [t_mode for t_mode in self.tunnel_modes]
        logging.info(
            '*** Supported tunnel modes: %s' % self.supported_tunnel_modes
        )

    """ Configure a tenant """

    def ConfigureTenant(self, request, context):
        logging.debug('Configure tenant request received: %s' % request)
        with RollbackContext() as rollback:
            # Extract tenant ID
            tenantid = request.tenantid
            # Extract tenant info
            tenant_info = request.tenant_info
            tenant_info = tenant_info if tenant_info != '' else None
            # Extract VXLAN port
            vxlan_port = request.config.vxlan_port
            vxlan_port = vxlan_port if vxlan_port != -1 else None
            # Parmeters validation
            #
            # Validate tenant ID
            logging.debug('Validating the tenant ID: %s' % tenantid)
            if not srv6_controller_utils.validate_tenantid(tenantid):
                # If tenant ID is invalid, return an error message
                err = 'Invalid tenant ID: %s' % tenantid
                logging.warning(err)
                return OverlayServiceReply(
                    status=Status(code=STATUS_BAD_REQUEST, reason=err)
                )
            # Validate VXLAN port
            if not srv6_controller_utils.validate_port(vxlan_port):
                # If VXLAN port is invalid, return an error message
                err = (
                    'Invalid VXLAN port %s for the tenant: %s'
                    % (vxlan_port, tenantid)
                )
                logging.warning(err)
                return OverlayServiceReply(
                    status=Status(code=STATUS_BAD_REQUEST, reason=err)
                )
            # Check if the tenant is configured
            is_config = storage_helper.is_tenant_configured(
                tenantid
            )
            if is_config and vxlan_port is not None:
                err = 'Cannot change the VXLAN port for a configured tenant'
                logging.error(err)
                return TenantReply(
                    status=Status(code=STATUS_BAD_REQUEST, reason=err)
                )
            # Configure the tenant
            vxlan_port = (
                vxlan_port if vxlan_port is not None else DEFAULT_VXLAN_PORT
            )
            storage_helper.configure_tenant(
                tenantid, tenant_info, vxlan_port
            )
            # TODO handle rollback?
            # Success, commit all performed operations
            rollback.commitAll()
        # Response
        return TenantReply(status=Status(code=STATUS_OK, reason='OK'))

    """ Remove a tenant """

    def RemoveTenant(self, request, context):
        logging.debug('Remove tenant request received: %s' % request)
        # Extract tenant ID
        tenantid = request.tenantid
        # Parmeters validation
        #
        # Validate tenant ID
        logging.debug('Validating the tenant ID: %s' % tenantid)
        if not srv6_controller_utils.validate_tenantid(tenantid):
            # If tenant ID is invalid, return an error message
            err = 'Invalid tenant ID: %s' % tenantid
            logging.warning(err)
            return OverlayServiceReply(
                status=Status(code=STATUS_BAD_REQUEST, reason=err)
            )
        # Remove the tenant
        #
        # Get all the overlays associated to the tenant ID
        overlays = storage_helper.get_overlays(tenantid=tenantid)
        if overlays is None:
            err = 'Error getting overlays'
            logging.error(err)
            return InventoryServiceReply(
                status=Status(code=STATUS_INTERNAL_SERVER_ERROR, reason=err)
            )
        # Remove all overlays
        for overlay in overlays:
            overlayid = overlay['_id']
            self._RemoveOverlay(overlayid, tenantid, tunnel_info=None)
        # Get all the devices of the tenant ID
        devices = storage_helper.get_devices(tenantid=tenantid)
        if devices is None:
            err = 'Error getting devices'
            logging.error(err)
            return OverlayServiceReply(
                status=Status(code=STATUS_INTERNAL_SERVER_ERROR, reason=err)
            )
        for device in devices:
            # Unregister all devices
            deviceid = device['deviceid']
            logging.debug('Unregistering device %s' % deviceid)
            self._unregister_device(deviceid, tenantid, ignore_errors=True)
        # TODO remove tenant from keystone
        #
        # Success
        return InventoryServiceReply(
            status=Status(code=STATUS_OK, reason='OK')
        )

    def enable_disable_device(self, deviceid, tenantid, enabled):
        # Enable/Disable the device
        res = storage_helper.set_device_enabled_flag(
            deviceid=deviceid, tenantid=tenantid, enabled=enabled
        )
        if res is None:
            err = (
                'Error while changing the enabled flag for the device %s: '
                'Unable to update the controller state' % deviceid
            )
            logging.error(err)
            return STATUS_INTERNAL_SERVER_ERROR, err
        elif res is False:
            err = (
                'Error while changing the enabled flag for the device %s: '
                % deviceid
            )
            logging.warning(err)
            return STATUS_BAD_REQUEST, err
        # Success
        return STATUS_OK, 'OK'

    """ Enable a device """

    def EnableDevice(self, request, context):
        logging.debug('EnableDevice request received: %s' % request)
        # Iterates on each device
        for device in request.devices:
            # Extract device ID
            deviceid = device.id
            # Extract tenant ID
            tenantid = device.tenantid
            # Enable the device
            status_code, reason = self.enable_disable_device(
                deviceid=deviceid, tenantid=tenantid, enabled=True
            )
            if status_code != STATUS_OK:
                # Error
                return OverlayServiceReply(
                    status=Status(code=status_code, reason=reason)
                )
        # Success: create the response
        return OverlayServiceReply(
            status=Status(code=STATUS_OK, reason='OK')
        )

    """ Enable a device """

    def DisableDevice(self, request, context):
        logging.debug('DisableDevice request received: %s' % request)
        # Iterates on each device
        for device in request.devices:
            # Extract device ID
            deviceid = device.id
            # Extract tenant ID
            tenantid = device.tenantid
            # Check tunnels stats
            # If the tenant has some overlays configured
            # it is not possible to unregister it
            num = storage_helper.get_num_tunnels(deviceid, tenantid)
            if num is None:
                err = (
                    'Error getting tunnels stats. Device not found '
                    'or error during the connection to the db'
                )
                logging.error(err)
                return OverlayServiceReply(
                    status=Status(
                        code=STATUS_INTERNAL_SERVER_ERROR, reason=err
                    )
                )
            elif num != 0:
                err = (
                    'Cannot disable the device. '
                    'The device has %s (tenant %s) has tunnels registered' %
                    (deviceid, tenantid)
                )
                logging.warning(err)
                return OverlayServiceReply(
                    status=Status(code=STATUS_BAD_REQUEST, reason=err)
                )
            # Disable the device
            status_code, reason = self.enable_disable_device(
                deviceid=deviceid, tenantid=tenantid, enabled=False
            )
            if status_code != STATUS_OK:
                # Error
                return OverlayServiceReply(
                    status=Status(code=status_code, reason=reason)
                )
        # Success: create the response
        return OverlayServiceReply(
            status=Status(code=STATUS_OK, reason='OK')
        )

    """ Configure a device and change its status to 'RUNNING' """

    def ConfigureDevice(self, request, context):
        logging.debug('ConfigureDevice request received: %s' % request)
        with RollbackContext() as rollback:
            # Get the devices
            devices = [device.id for device in request.configuration.devices]
            devices = storage_helper.get_devices(
                deviceids=devices, return_dict=True
            )
            if devices is None:
                logging.error('Error getting devices')
                return OverlayServiceReply(
                    status=Status(
                        code=STATUS_INTERNAL_SERVER_ERROR,
                        reason='Error getting devices'
                    )
                )
            # Convert interfaces list to a dict representation
            # This step simplifies future processing
            interfaces = dict()
            for deviceid in devices:
                for interface in devices[deviceid]['interfaces']:
                    interfaces[interface['name']] = interface
                devices[deviceid]['interfaces'] = interfaces
            # Parameters validation
            for device in request.configuration.devices:
                # Parameters extraction
                #
                # Extract the device ID from the configuration
                deviceid = device.id
                # Extract the tenant ID
                tenantid = device.tenantid
                # Extract the interfaces
                interfaces = device.interfaces
                # Extract the device name
                device_name = device.name
                # Extract the device description
                device_description = device.description
                # If the device is partecipating to some overlay
                # we cannot configure it
                overlay = storage_helper.get_overlay_containing_device(
                    deviceid, tenantid
                )
                if overlay is not None:
                    err = (
                        'Cannot configure device %s: the device '
                        'is partecipating to the overlay %s' %
                        (deviceid, overlay['_id'])
                    )
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Name is mandatory
                if device_name is None or device_name == '':
                    err = (
                        'Invalid configuration for device %s\n'
                        'Invalid value for the mandatory parameter '
                        '"name": %s' % (deviceid, device_name)
                    )
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Description parameter is mandatory
                if device_description is None or device_description == '':
                    err = (
                        'Invalid configuration for device %s\n'
                        'Invalid value for the mandatory parameter '
                        '"description": %s' % (deviceid, device_description)
                    )
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Validate the device IDs
                logging.debug('Validating the device ID: %s' % deviceid)
                if not srv6_controller_utils.validate_deviceid(deviceid):
                    # If device ID is invalid, return an error message
                    err = (
                        'Invalid configuration for device %s\n'
                        'Invalid device ID: %s' % (deviceid, deviceid)
                    )
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Validate the tenant ID
                logging.debug('Validating the tenant ID: %s' % tenantid)
                if not srv6_controller_utils.validate_tenantid(tenantid):
                    # If tenant ID is invalid, return an error message
                    err = (
                        'Invalid configuration for device %s\n'
                        'Invalid tenant ID: %s' % (deviceid, tenantid)
                    )
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Check if the devices exist
                if deviceid not in devices:
                    err = (
                        'Invalid configuration for device %s\n'
                        'Device not found: %s' % (deviceid, deviceid)
                    )
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Check if the device belongs to the tenant
                if tenantid != devices[deviceid]['tenantid']:
                    err = (
                        'Invalid configuration for device %s\n'
                        'The device %s does not belong to the tenant %s' %
                        (deviceid, deviceid, tenantid)
                    )
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Validate the interfaces
                wan_interfaces_counter = 0
                lan_interfaces_counter = 0
                for interface in interfaces:
                    # Update counters
                    if interface.type == InterfaceType.WAN:
                        wan_interfaces_counter += 1
                    elif interface.type == InterfaceType.LAN:
                        lan_interfaces_counter += 1
                    # Check if the interface exists
                    if interface.name not in devices[deviceid]['interfaces']:
                        err = (
                            'Invalid configuration for device %s\n'
                            'Interface %s not found on device %s'
                            % (deviceid, interface.name, deviceid)
                        )
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check interface type
                    if not srv6_controller_utils.validate_interface_type(
                        interface.type
                    ):
                        err = (
                            'Invalid configuration for device %s\n'
                            'Invalid type %s for the interface %s (%s)' %
                            (deviceid, interface.type, interface.name,
                             deviceid)
                        )
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Cannot set IP address and subnets for the WAN interfaces
                    if interface.type == InterfaceType.WAN:
                        if len(interface.ipv4_addrs) > 0 or \
                                len(interface.ipv6_addrs) > 0:
                            err = (
                                'Invalid configuration for device %s\n'
                                'WAN interfaces do not support IP addrs '
                                'assignment: %s' % (deviceid, interface.name)
                            )
                            logging.warning(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                        if len(interface.ipv4_subnets) > 0 or \
                                len(interface.ipv6_subnets) > 0:
                            err = (
                                'Invalid configuration for device %s\n'
                                'WAN interfaces do not support subnets '
                                'assignment: %s' % (deviceid, interface.name)
                            )
                            logging.warning(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                    # Validate IP addresses
                    for ipaddr in interface.ipv4_addrs:
                        if not srv6_controller_utils.validate_ipv4_address(
                            ipaddr
                        ):
                            err = (
                                'Invalid configuration for device %s\n'
                                'Invalid IPv4 address %s for the interface %s'
                                % (deviceid, ipaddr, interface.name)
                            )
                            logging.warning(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                    for ipaddr in interface.ipv6_addrs:
                        if not srv6_controller_utils.validate_ipv6_address(
                            ipaddr
                        ):
                            err = (
                                'Invalid configuration for device %s\n'
                                'Invalid IPv6 address %s for the '
                                'interface %s' %
                                (deviceid, ipaddr, interface.name)
                            )
                            logging.warning(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                    # Validate subnets
                    for subnet in interface.ipv4_subnets:
                        gateway = subnet.gateway
                        subnet = subnet.subnet
                        if not srv6_controller_utils.validate_ipv4_address(
                            subnet
                        ):
                            err = (
                                'Invalid configuration for device %s\n'
                                'Invalid IPv4 subnet %s for the interface %s' %
                                (deviceid, subnet, interface.name)
                            )
                            logging.warning(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                        if gateway is not None and gateway != '':
                            if (not srv6_controller_utils
                                    .validate_ipv4_address(gateway)):
                                err = (
                                    'Invalid configuration for device %s\n'
                                    'Invalid IPv4 gateway %s for the '
                                    'subnet %s on the interface %s' %
                                    (deviceid, gateway, subnet, interface.name)
                                )
                                logging.warning(err)
                                return OverlayServiceReply(
                                    status=Status(
                                        code=STATUS_BAD_REQUEST, reason=err
                                    )
                                )
                    for subnet in interface.ipv6_subnets:
                        gateway = subnet.gateway
                        subnet = subnet.subnet
                        if not srv6_controller_utils.validate_ipv6_address(
                            subnet
                        ):
                            err = (
                                'Invalid configuration for device %s\n'
                                'Invalid IPv6 subnet %s for the interface %s' %
                                (deviceid, subnet, interface.name)
                            )
                            logging.warning(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                        if gateway is not None and gateway != '':
                            if not srv6_controller_utils.validate_ipv6_address(
                                gateway
                            ):
                                err = (
                                    'Invalid configuration for device %s\n'
                                    'Invalid IPv6 gateway %s for the '
                                    'subnet %s on the interface %s' %
                                    (deviceid, gateway, subnet, interface.name)
                                )
                                logging.warning(err)
                                return OverlayServiceReply(
                                    status=Status(
                                        code=STATUS_BAD_REQUEST, reason=err
                                    )
                                )
                # At least one WAN interface is required
                if wan_interfaces_counter == 0:
                    err = (
                        'Invalid configuration for device %s\n'
                        'The configuration must contain at least one WAN '
                        'interface (0 provided)' % deviceid
                    )
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # At least one LAN interface is required
                if lan_interfaces_counter == 0:
                    err = (
                        'Invalid configuration for device %s\n'
                        'The configuration must contain at least one LAN '
                        'interface (0 provided)' % deviceid
                    )
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
            # All checks passed
            #
            # Remove curent STAMP information
            if ENABLE_STAMP_SUPPORT:
                logging.info('Removing current STAMP information\n\n')
                for device in request.configuration.devices:
                    # Extract the device ID from the configuration
                    deviceid = device.id
                    # Configure information
                    try:
                        stamp_node = (
                            self.stamp_controller.storage.get_stamp_node(
                                node_id=deviceid, tenantid=tenantid
                            )
                        )
                        if stamp_node is not None:
                            self.stamp_controller.remove_stamp_node(
                                node_id=deviceid, tenantid=tenantid
                            )
                            # Add reverse action to the rollback stack
                            rollback.push(
                                func=exec_or_mark_device_inconsitent,
                                rollback_func=(
                                    self.stamp_controller.add_stamp_node
                                ),
                                node_id=stamp_node.node_id,
                                node_name=stamp_node.node_name,
                                grpc_ip=stamp_node.grpc_ip,
                                grpc_port=stamp_node.grpc_port,
                                ip=stamp_node.ip,
                                sender_port=stamp_node.sender_udp_port,
                                reflector_port=stamp_node.reflector_udp_port,
                                interfaces=stamp_node.interfaces,
                                stamp_source_ipv6_address=(
                                    stamp_node.stamp_source_ipv6_address
                                ),
                                is_sender=stamp_node.is_sender,
                                is_reflector=stamp_node.is_reflector,
                                deviceid=deviceid,
                                tenantid=tenantid
                            )
                    except NodeIdNotFoundError:
                        logging.debug(
                            f'STAMP Node {deviceid} does not exist. '
                            'Nothing to do.'
                        )
                    except STAMPSessionsExistError:
                        err = (
                            f'STAMP Node {deviceid} is participating in one '
                            'or more STAMP sessions. Delete all existing '
                            'sessions before changing device configuration.'
                        )
                        logging.error(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
            # Extract the configurations from the request message
            new_devices = list()
            for device in request.configuration.devices:
                logging.info('Processing the configuration:\n%s' % device)
                # Parameters extraction
                #
                # Extract the device ID from the configuration
                deviceid = device.id
                # Extract the device name from the configuration
                device_name = device.name
                # Extract the device description from the configuration
                device_description = device.description
                # Extract the tenant ID
                tenantid = device.tenantid
                # Extract the device interfaces from the configuration
                interfaces = devices[deviceid]['interfaces']
                err = STATUS_OK
                for interface in device.interfaces:
                    interfaces[interface.name]['name'] = interface.name
                    if interface.type != '':
                        interfaces[interface.name]['type'] = interface.type
                    if interface.type == InterfaceType.WAN:
                        if len(interface.ipv4_addrs) > 0 or \
                                len(interface.ipv6_addrs) > 0:
                            logging.warning(
                                'Cannot set IP addrs for a WAN interface'
                            )
                        if len(interface.ipv4_subnets) > 0 or \
                                len(interface.ipv6_subnets) > 0:
                            logging.warning(
                                'Cannot set subnets for a WAN interface'
                            )
                    else:
                        if len(interface.ipv4_addrs) > 0:
                            addrs = list()
                            for addr in interfaces[
                                interface.name
                            ]['ipv4_addrs']:
                                addrs.append(addr)
                                response = self.srv6_manager.remove_ipaddr(
                                    devices[deviceid]['mgmtip'],
                                    self.grpc_client_port,
                                    ip_addr=addr,
                                    device=interface.name,
                                    family=AF_UNSPEC
                                )
                                if response != SbStatusCode.STATUS_SUCCESS:
                                    # If the operation has failed,
                                    # report an error message
                                    logging.warning(
                                        'Cannot remove the public addresses '
                                        'from the interface'
                                    )
                                    err = (
                                        status_codes_pb2.STATUS_INTERNAL_ERROR
                                    )
                                # Add reverse action to the rollback stack
                                rollback.push(
                                    func=exec_or_mark_device_inconsitent,
                                    rollback_func=(
                                        self.srv6_manager.create_ipaddr
                                    ),
                                    server_ip=devices[deviceid]['mgmtip'],
                                    server_port=self.grpc_client_port,
                                    ip_addr=addr,
                                    device=interface.name,
                                    family=AF_INET,
                                    deviceid=deviceid,
                                    tenantid=tenantid
                                )
                            interfaces[interface.name]['ipv4_addrs'] = list()
                            # Add IP address to the interface
                            for ipv4_addr in interface.ipv4_addrs:
                                response = self.srv6_manager.create_ipaddr(
                                    devices[deviceid]['mgmtip'],
                                    self.grpc_client_port,
                                    ip_addr=ipv4_addr,
                                    device=interface.name,
                                    family=AF_INET
                                )
                                if response != SbStatusCode.STATUS_SUCCESS:
                                    # If the operation has failed,
                                    # report an error message
                                    logging.warning(
                                        'Cannot assign the private VPN IP '
                                        'address to the interface'
                                    )
                                    err = (
                                        status_codes_pb2.STATUS_INTERNAL_ERROR
                                    )
                                interfaces[interface.name][
                                    'ipv4_addrs'].append(ipv4_addr)
                                # Add reverse action to the rollback stack
                                rollback.push(
                                    func=exec_or_mark_device_inconsitent,
                                    rollback_func=(
                                        self.srv6_manager.remove_ipaddr
                                    ),
                                    server_ip=devices[deviceid]['mgmtip'],
                                    server_port=self.grpc_client_port,
                                    ip_addr=ipv4_addr,
                                    device=interface.name,
                                    family=AF_INET,
                                    deviceid=deviceid,
                                    tenantid=tenantid
                                )
                        if len(interface.ipv6_addrs) > 0:
                            addrs = list()
                            nets = list()
                            for addr in interfaces[
                                interface.name
                            ]['ipv6_addrs']:
                                addrs.append(addr)
                                nets.append(str(IPv6Interface(addr).network))
                                response = self.srv6_manager.remove_ipaddr(
                                    devices[deviceid]['mgmtip'],
                                    self.grpc_client_port,
                                    ip_addr=addr,
                                    net=str(IPv6Interface(addr).network),
                                    device=interface.name,
                                    family=AF_UNSPEC
                                )
                                if response != SbStatusCode.STATUS_SUCCESS:
                                    # If the operation has failed,
                                    # report an error message
                                    logging.warning(
                                        'Cannot remove the public addresses '
                                        'from the interface'
                                    )
                                    err = (
                                        status_codes_pb2.STATUS_INTERNAL_ERROR
                                    )
                                # Add reverse action to the rollback stack
                                rollback.push(
                                    func=exec_or_mark_device_inconsitent,
                                    rollback_func=(
                                        self.srv6_manager.create_ipaddr
                                    ),
                                    server_ip=devices[deviceid]['mgmtip'],
                                    server_port=self.grpc_client_port,
                                    ip_addr=addr,
                                    device=interface.name,
                                    family=AF_INET6,
                                    net=str(IPv6Interface(addr).network),
                                    deviceid=deviceid,
                                    tenantid=tenantid
                                )
                            interfaces[interface.name]['ipv6_addrs'] = list()
                            # Add IP address to the interface
                            for ipv6_addr in interface.ipv6_addrs:
                                net = IPv6Interface(
                                    ipv6_addr
                                ).network.__str__()
                                response = self.srv6_manager.create_ipaddr(
                                    devices[deviceid]['mgmtip'],
                                    self.grpc_client_port,
                                    ip_addr=ipv6_addr,
                                    device=interface.name,
                                    net=net,
                                    family=AF_INET6
                                )
                                if response != SbStatusCode.STATUS_SUCCESS:
                                    # If the operation has failed,
                                    # report an error message
                                    logging.warning(
                                        'Cannot assign the private VPN IP '
                                        'address to the interface'
                                    )
                                    err = (
                                        status_codes_pb2.STATUS_INTERNAL_ERROR
                                    )
                                # Add reverse action to the rollback stack
                                rollback.push(
                                    func=exec_or_mark_device_inconsitent,
                                    rollback_func=(
                                        self.srv6_manager.remove_ipaddr
                                    ),
                                    server_ip=devices[deviceid]['mgmtip'],
                                    server_port=self.grpc_client_port,
                                    ip_addr=ipv6_addr,
                                    device=interface.name,
                                    family=AF_INET6,
                                    net=str(IPv6Interface(addr).network),
                                    deviceid=deviceid,
                                    tenantid=tenantid
                                )
                                interfaces[
                                    interface.name
                                ]['ipv6_addrs'].append(ipv6_addr)
                        interfaces[interface.name]['ipv4_subnets'] = list()
                        for subnet in interface.ipv4_subnets:
                            gateway = subnet.gateway
                            subnet = subnet.subnet
                            interfaces[interface.name]['ipv4_subnets'].append(
                                {'subnet': subnet, 'gateway': gateway}
                            )
                        interfaces[interface.name]['ipv6_subnets'] = list()
                        for subnet in interface.ipv6_subnets:
                            gateway = subnet.gateway
                            subnet = subnet.subnet
                            interfaces[interface.name]['ipv6_subnets'].append(
                                {'subnet': subnet, 'gateway': gateway}
                            )
                # Push the new configuration
                if err == STATUS_OK:
                    logging.debug(
                        'The device %s has been configured successfully',
                        deviceid
                    )
                    new_devices.append(
                        {
                            'deviceid': deviceid,
                            'name': device_name,
                            'description': device_description,
                            'interfaces': interfaces,
                            'tenantid': tenantid,
                            'configured': True
                        }
                    )
                else:
                    err = 'The device %s rejected the configuration' % deviceid
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
            success = storage_helper.configure_devices(new_devices)
            if success is False or success is None:
                err = 'Error configuring the devices'
                logging.error(err)
                return OverlayServiceReply(
                    status=Status(
                        code=STATUS_INTERNAL_SERVER_ERROR, reason=err
                    )
                )
            logging.info('The device configuration has been saved\n\n')
            # Setup STAMP information
            if ENABLE_STAMP_SUPPORT:
                logging.info('Configuring STAMP information\n\n')
                for device in request.configuration.devices:
                    # Extract the device ID from the configuration
                    deviceid = device.id
                    # Extract the tenant ID
                    tenantid = device.tenantid
                    # Retrieve device information
                    device = storage_helper.get_device(
                        deviceid=deviceid, tenantid=tenantid
                    )
                    if device is None:
                        logging.error('Error getting device')
                        return OverlayServiceReply(
                            status=Status(
                                code=STATUS_INTERNAL_SERVER_ERROR,
                                reason='Error getting device'
                            )
                        )
                    # Lookup the WAN interfaces
                    # TODO currently we only support a single WAN interface,
                    # so we look for the address of the first WAN interface
                    # In the future we should support multiple interfaces
                    wan_ip = None
                    wan_ifaces = None
                    for interface in device['interfaces']:
                        if interface['type'] == InterfaceType.WAN and \
                                len(interface['ipv6_addrs']) > 0:
                            wan_ip = interface['ipv6_addrs'][0].split('/')[0]
                            wan_ifaces = [interface['name']]
                            break
                    # Configure information
                    self.stamp_controller.add_stamp_node(
                        node_id=device['deviceid'],
                        node_name=device['name'],
                        grpc_ip=device['mgmtip'],
                        grpc_port=self.grpc_client_port,
                        ip=wan_ip,
                        sender_port=42069,
                        reflector_port=862,
                        interfaces=wan_ifaces,
                        stamp_source_ipv6_address=wan_ip,
                        is_sender=True,
                        is_reflector=True,
                        tenantid=tenantid
                    )
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=exec_or_mark_device_inconsitent,
                        rollback_func=self.stamp_controller.remove_stamp_node,
                        node_id=device['deviceid'],
                        deviceid=device['deviceid'],
                        tenantid=tenantid
                    )
            # Success, commit all performed operations
            rollback.commitAll()
        # Create the response
        return OverlayServiceReply(
            status=Status(code=STATUS_OK, reason='OK')
        )

    """ Get the registered devices """

    def GetDevices(self, request, context):
        logging.debug('GetDeviceInformation request received')
        # Extract the device IDs from the request
        deviceids = list(request.deviceids)
        deviceids = deviceids if len(deviceids) > 0 else None
        # Extract the tenant ID from the request
        tenantid = request.tenantid
        tenantid = tenantid if tenantid != '' else None
        # Parameters validation
        #
        # Validate the device IDs
        if deviceids is not None:
            for deviceid in deviceids:
                logging.debug('Validating the device ID: %s' % deviceid)
                if not srv6_controller_utils.validate_deviceid(deviceid):
                    # If device ID is invalid, return an error message
                    err = 'Invalid device ID: %s' % deviceid
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
        # Validate the tenant ID
        if tenantid is not None:
            logging.debug('Validating the tenant ID: %s' % tenantid)
            if not srv6_controller_utils.validate_tenantid(tenantid):
                # If tenant ID is invalid, return an error message
                err = 'Invalid tenant ID: %s' % tenantid
                logging.warning(err)
                return OverlayServiceReply(
                    status=Status(code=STATUS_BAD_REQUEST, reason=err)
                )
        # Create the response
        response = srv6_vpn_pb2.InventoryServiceReply()
        # Iterate on devices and fill the response message
        devices = storage_helper.get_devices(
            deviceids=deviceids, tenantid=tenantid
        )
        if devices is None:
            err = 'Error getting devices'
            logging.error(err)
            return OverlayServiceReply(
                status=Status(code=STATUS_INTERNAL_SERVER_ERROR, reason=err)
            )
        for _device in devices:
            device = response.device_information.devices.add()
            device.id = text_type(_device['deviceid'])
            _interfaces = _device.get('interfaces', [])
            for ifinfo in _interfaces:
                interface = device.interfaces.add()
                interface.name = ifinfo['name']
                interface.mac_addr = ifinfo['mac_addr']
                interface.ipv4_addrs.extend(ifinfo['ipv4_addrs'])
                interface.ipv6_addrs.extend(ifinfo['ipv6_addrs'])
                interface.ext_ipv4_addrs.extend(ifinfo['ext_ipv4_addrs'])
                interface.ext_ipv6_addrs.extend(ifinfo['ext_ipv6_addrs'])
                for _subnet in ifinfo['ipv4_subnets']:
                    subnet = interface.ipv4_subnets.add()
                    subnet.subnet = _subnet['subnet']
                    subnet.gateway = _subnet['gateway']
                for _subnet in ifinfo['ipv6_subnets']:
                    subnet = interface.ipv6_subnets.add()
                    subnet.subnet = _subnet['subnet']
                    subnet.gateway = _subnet['gateway']
                interface.type = ifinfo['type']
            mgmtip = _device.get('mgmtip')
            name = _device.get('name')
            description = _device.get('description')
            connected = _device.get('connected')
            configured = _device.get('configured')
            enabled = _device.get('enabled')
            if mgmtip is not None:
                device.mgmtip = mgmtip
            if name is not None:
                device.name = name
            if description is not None:
                device.description = description
            if connected is not None:
                device.connected = connected
            if configured is not None:
                device.configured = configured
            if enabled is not None:
                device.enabled = enabled
        # Return the response
        logging.debug('Sending response:\n%s' % response)
        response.status.code = STATUS_OK
        response.status.reason = 'OK'
        return response

    """ Get the topology information """

    def GetTopologyInformation(self, request, context):
        logging.debug('GetTopologyInformation request received')
        # Create the response
        response = srv6_vpn_pb2.InventoryServiceReply()
        # Build the topology
        topology = storage_helper.get_topology()
        if topology is None:
            err = 'Error getting the topology'
            logging.error(err)
            return OverlayServiceReply(
                status=Status(
                    code=STATUS_INTERNAL_SERVER_ERROR,
                    reason=err
                )
            )
        nodes = topology['nodes']
        links = topology['links']
        devices = set()
        # Iterate on nodes
        for node in nodes:
            if node['type'] != 'router':
                # Skip stub networks
                continue
            devices.add(node['id'])
            response.topology_information.devices.append(node['id'])
        # Iterate on links
        for _link in links:
            if _link[0] in devices and _link[1] in devices:
                link = response.topology_information.links.add()
                link.l_device = _link[0]
                link.r_device = _link[1]
        # Return the response
        logging.debug('Sending response:\n%s' % response)
        response.status.code = STATUS_OK
        response.status.reason = 'OK'
        return response

    def _unregister_device(self, deviceid, tenantid, ignore_errors=False):
        # Parameters validation
        #
        # Validate the tenant ID
        logging.debug('Validating the tenant ID: %s' % tenantid)
        tenant_exists = storage_helper.tenant_exists(tenantid)
        if tenant_exists is None:
            err = 'Error while connecting to the controller state'
            logging.error(err)
            STATUS_INTERNAL_SERVER_ERROR, err
        elif tenant_exists is False:
            # If tenant ID is invalid, return an error message
            err = 'Tenant not found: %s' % tenantid
            logging.warning(err)
            return STATUS_BAD_REQUEST, err
        # Validate the device ID
        logging.debug('Validating the device ID: %s' % tenantid)
        devices = storage_helper.get_devices(
            deviceids=[deviceid]
        )
        if devices is None:
            err = 'Error getting devices'
            logging.error(err)
            return STATUS_INTERNAL_SERVER_ERROR, err
        elif len(devices) == 0:
            # If device ID is invalid, return an error message
            err = 'Device not found: %s' % deviceid
            logging.warning(err)
            return STATUS_BAD_REQUEST, err
        # The device must belong to the tenant
        device = devices[0]
        if device['tenantid'] != tenantid:
            err = (
                'Cannot unregister the device. '
                'The device %s does not belong to the tenant %s' %
                (deviceid, tenantid)
            )
            logging.warning(err)
            return STATUS_BAD_REQUEST, err
        # Check tunnels stats
        # If the tenant has some overlays configured
        # it is not possible to unregister it
        num = storage_helper.get_num_tunnels(deviceid, tenantid)
        if num is None:
            err = 'Error getting tunnels stats'
            logging.error(err)
            return STATUS_INTERNAL_SERVER_ERROR, err
        elif num != 0:
            err = (
                'Cannot unregister the device %s. '
                'The device has %s tunnels registered' %
                (deviceid, num)
            )
            logging.warning(err)
            return STATUS_BAD_REQUEST, err
        # All checks passed
        #
        # Remove curent STAMP information
        if ENABLE_STAMP_SUPPORT:
            logging.info('Removing current STAMP information\n\n')
            # Configure information
            try:
                stamp_node = (
                    self.stamp_controller.storage.get_stamp_node(
                        node_id=deviceid, tenantid=tenantid
                    )
                )
                if stamp_node is not None:
                    self.stamp_controller.remove_stamp_node(
                        node_id=deviceid, tenantid=tenantid
                    )
            except NodeIdNotFoundError:
                logging.debug(
                    f'STAMP Node {deviceid} does not exist. '
                    'Nothing to do.'
                )
            except STAMPSessionsExistError:
                err = (
                    f'STAMP Node {deviceid} is participating in one '
                    'or more STAMP sessions. Delete all existing '
                    'sessions before changing device configuration.'
                )
                logging.error(err)
                return STATUS_BAD_REQUEST, err
            except grpc.RpcError:
                if ignore_errors:
                    err = (
                        'Unregister STAMP information failed. Setting reboot '
                        'required flag.'
                    )
                    logging.warning(err)
                    # Change device state to reboot required
                    success = storage_helper.change_device_state(
                        deviceid=deviceid,
                        tenantid=tenantid,
                        new_state=storage_helper.DeviceState.REBOOT_REQUIRED
                    )
                    if success is False or success is None:
                        logging.error('Error changing the device state')
                        return status_codes_pb2.STATUS_INTERNAL_ERROR
                else:
                    err = (
                        'Cannot unregister the device. '
                        'Error while unregistering STAMP information'
                    )
                    logging.error(err)
                    return STATUS_INTERNAL_SERVER_ERROR, err
        # Let's unregister the device
        #
        # Send shutdown command to device
        res = self.srv6_manager.shutdown_device(
            device['mgmtip'], self.grpc_client_port
        )
        if res != SbStatusCode.STATUS_SUCCESS:
            if ignore_errors:
                err = ('Device shutdown failed. Setting reboot required flag.')
                logging.warning(err)
                # Change device state to reboot required
                success = storage_helper.change_device_state(
                    deviceid=deviceid,
                    tenantid=tenantid,
                    new_state=storage_helper.DeviceState.REBOOT_REQUIRED
                )
                if success is False or success is None:
                    logging.error('Error changing the device state')
                    return status_codes_pb2.STATUS_INTERNAL_ERROR
            else:
                err = (
                    'Cannot unregister the device. '
                    'Error while shutting down the device'
                )
                logging.error(err)
                return STATUS_INTERNAL_SERVER_ERROR, err
        # Remove device from controller state
        success = storage_helper.unregister_device(
            deviceid, tenantid
        )
        if success is None or success is False:
            err = (
                'Cannot unregister the device. '
                'Error while updating the controller state'
            )
            logging.error(err)
            return STATUS_INTERNAL_SERVER_ERROR, err
        # Remove node from STAMP inventory
        stamp_node = self.stamp_controller.storage.get_stamp_node(
            node_id=deviceid, tenantid=tenantid
        )
        if stamp_node is not None:
            try:
                self.stamp_controller.remove_stamp_node(
                    node_id=deviceid, tenantid=tenantid
                )
            except Exception:  # as err:
                # TODO replace with a more specific exception
                self.stamp_controller.storage.remove_stamp_node(
                    node_id=deviceid, tenantid=tenantid
                )
        # Success
        logging.info('Device unregistered successfully\n\n')
        return STATUS_OK, 'OK'

    """ Unregister a device """

    def UnregisterDevice(self, request, context):
        logging.info('UnregisterDevice request received:\n%s', request)
        # Parameters extraction
        #
        # Extract the tenant ID
        tenantid = request.tenantid
        # Extract the device ID
        deviceid = request.deviceid
        # Unregister the device
        code, reason = self._unregister_device(
            deviceid, tenantid, ignore_errors=True
        )
        # Create the response
        return OverlayServiceReply(status=Status(code=code, reason=reason))

    """Create a VPN from an intent received through the northbound interface"""

    def CreateOverlay(self, request, context):
        logging.info('CreateOverlay request received:\n%s', request)
        with RollbackContext() as rollback:
            # Extract the intents from the request message
            for intent in request.intents:
                logging.info('Processing the intent:\n%s' % intent)
                # Parameters extraction
                #
                # Extract the overlay tenant ID from the intent
                tenantid = intent.tenantid
                # Extract the overlay type from the intent
                overlay_type = intent.overlay_type
                # Extract the overlay name from the intent
                overlay_name = intent.overlay_name
                # Extract the interfaces
                slices = list()
                _devices = set()
                for _slice in intent.slices:
                    deviceid = _slice.deviceid
                    interface_name = _slice.interface_name
                    # Add the slice to the slices set
                    slices.append(
                        {
                            'deviceid': deviceid,
                            'interface_name': interface_name
                        }
                    )
                    # Add the device to the devices set
                    _devices.add(deviceid)
                # Extract tunnel mode
                tunnel_name = intent.tunnel_mode
                # Extract tunnel info
                tunnel_info = intent.tunnel_info
                # Parameters validation
                #
                # Validate the tenant ID
                logging.debug('Validating the tenant ID: %s' % tenantid)
                if not srv6_controller_utils.validate_tenantid(tenantid):
                    # If tenant ID is invalid, return an error message
                    err = 'Invalid tenant ID: %s' % tenantid
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Check if the tenant is configured
                is_config = storage_helper.is_tenant_configured(
                    tenantid
                )
                if is_config is None:
                    err = 'Error while checking tenant configuration'
                    logging.error(err)
                    return TenantReply(
                        status=Status(
                            code=STATUS_INTERNAL_SERVER_ERROR,
                            reason=err
                        )
                    )
                elif is_config is False:
                    err = (
                        'Cannot create overlay for a tenant unconfigured'
                        'Tenant not found or error during the '
                        'connection to the db'
                    )
                    logging.warning(err)
                    return TenantReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Validate the overlay type
                logging.debug('Validating the overlay type: %s' % overlay_type)
                if not srv6_controller_utils.validate_overlay_type(
                    overlay_type
                ):
                    # If the overlay type is invalid, return an error message
                    err = 'Invalid overlay type: %s' % overlay_type
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Validate the overlay name
                logging.debug('Validating the overlay name: %s' % overlay_name)
                if not srv6_controller_utils.validate_overlay_name(
                    overlay_name
                ):
                    # If the overlay name is invalid, return an error message
                    err = 'Invalid overlay name: %s' % overlay_name
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Validate the tunnel mode
                logging.debug('Validating the tunnel mode: %s' % tunnel_name)
                if not srv6_controller_utils.validate_tunnel_mode(
                        tunnel_name, self.supported_tunnel_modes
                ):
                    # If the tunnel mode is invalid, return an error message
                    err = 'Invalid tunnel mode: %s' % tunnel_name
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Let's check if the overlay does not exist
                logging.debug(
                    'Checking if the overlay name is available: %s' %
                    overlay_name
                )
                exists = storage_helper.overlay_exists(
                    overlay_name, tenantid
                )
                if exists is True:
                    # If the overlay already exists, return an error message
                    err = (
                        'Overlay name %s is already in use for tenant %s' %
                        (overlay_name, tenantid)
                    )
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                elif exists is None:
                    err = 'Error validating the overlay'
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(
                            code=STATUS_INTERNAL_SERVER_ERROR, reason=err
                        )
                    )
                # Get the devices
                devices = storage_helper.get_devices(
                    deviceids=_devices, return_dict=True
                )
                if devices is None:
                    err = 'Error getting devices'
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(
                            code=STATUS_INTERNAL_SERVER_ERROR, reason=err
                        )
                    )
                # Devices validation
                for deviceid in devices:
                    # Let's check if the router exists
                    if deviceid not in devices:
                        # If the device does not exist, return an error message
                        err = 'Device not found %s' % deviceid
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check if the device is connected
                    if not devices[deviceid]['connected']:
                        # If the device is not connected, return an error
                        # message
                        err = 'The device %s is not connected' % deviceid
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check if the device is enabled
                    if not devices[deviceid]['enabled']:
                        # If the device is not enabled, return an error message
                        err = 'The device %s is not enabled' % deviceid
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check if the devices have at least a WAN interface
                    wan_found = False
                    for interface in devices[deviceid]['interfaces']:
                        if interface['type'] == InterfaceType.WAN:
                            wan_found = True
                    if not wan_found:
                        # No WAN interfaces found on the device
                        err = (
                            'No WAN interfaces found on the device %s'
                            % deviceid
                        )
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                # Convert interfaces list to a dict representation
                # This step simplifies future processing
                interfaces = dict()
                for deviceid in devices:
                    for interface in devices[deviceid]['interfaces']:
                        interfaces[interface['name']] = interface
                    devices[deviceid]['interfaces'] = interfaces
                # Validate the slices included in the intent
                for _slice in slices:
                    logging.debug('Validating the slice: %s' % _slice)
                    # A slice is a tuple (deviceid, interface_name)
                    #
                    # Extract the device ID
                    deviceid = _slice['deviceid']
                    # Extract the interface name
                    interface_name = _slice['interface_name']
                    # Let's check if the router exists
                    if deviceid not in devices:
                        # If the device does not exist, return an error
                        # message
                        err = 'Device not found %s' % deviceid
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check if the device is enabled
                    if not devices[deviceid]['enabled']:
                        # If the device is not enabled, return an error
                        # message
                        err = 'The device %s is not enabled' % deviceid
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check if the device is connected
                    if not devices[deviceid]['connected']:
                        # If the device is not connected, return an error
                        # message
                        err = 'The device %s is not connected' % deviceid
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Let's check if the interface exists
                    if interface_name not in devices[deviceid]['interfaces']:
                        # If the interface does not exists, return an error
                        # message
                        err = 'The interface does not exist'
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check if the interface type is LAN
                    if devices[deviceid]['interfaces'][
                        interface_name
                    ]['type'] != InterfaceType.LAN:
                        # The interface type is not LAN
                        err = (
                            'Cannot add non-LAN interface to the overlay: %s '
                            '(device %s)' % (interface_name, deviceid)
                        )
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check if the slice is already assigned to an overlay
                    _overlay = storage_helper.get_overlay_containing_slice(
                        _slice, tenantid
                    )
                    if _overlay is not None:
                        # Slice already assigned to an overlay
                        err = (
                            'Cannot create overlay: the slice %s is '
                            'already assigned to the overlay %s' %
                            (_slice, _overlay['_id'])
                        )
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check for IP addresses
                    if overlay_type == OverlayType.IPv4Overlay:
                        addrs = storage_helper.get_ipv4_addresses(
                            deviceid=deviceid,
                            tenantid=tenantid,
                            interface_name=interface_name
                        )
                        if len(addrs) == 0:
                            # No IPv4 address assigned to the interface
                            err = (
                                'Cannot create overlay: the slice %s has no '
                                'IPv4 addresses; at least one IPv4 address '
                                'is required to create an IPv4 Overlay'
                                % _slice
                            )
                            logging.error(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                        subnets = storage_helper.get_ipv4_subnets(
                            deviceid=deviceid,
                            tenantid=tenantid,
                            interface_name=interface_name
                        )
                        if len(subnets) == 0:
                            # No IPv4 subnet assigned to the interface
                            err = (
                                'Cannot create overlay: the slice %s has no '
                                'IPv4 subnets; at least one IPv4 subnet is '
                                'required to create an IPv4 Overlay'
                                % _slice
                            )
                            logging.error(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                    elif overlay_type == OverlayType.IPv6Overlay:
                        addrs = storage_helper.get_ipv6_addresses(
                            deviceid=deviceid,
                            tenantid=tenantid,
                            interface_name=interface_name
                        )
                        if len(addrs) == 0:
                            # No IPv6 address assigned to the interface
                            err = (
                                'Cannot create overlay: the slice %s has no '
                                'IPv6 addresses; at least one IPv6 address '
                                'is required to create an IPv6 Overlay'
                                % _slice
                            )
                            logging.error(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                        subnets = storage_helper.get_ipv6_subnets(
                            deviceid=deviceid,
                            tenantid=tenantid,
                            interface_name=interface_name
                        )
                        if len(subnets) == 0:
                            # No IPv6 subnet assigned to the interface
                            err = (
                                'Cannot create overlay: the slice %s has no '
                                'IPv6 subnets; at least one IPv6 subnet is '
                                'required to create an IPv6 Overlay'
                                % _slice
                            )
                            logging.error(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                for slice1 in slices:
                    # Extract the device ID
                    deviceid_1 = slice1['deviceid']
                    # Extract the interface name
                    interface_name_1 = slice1['interface_name']
                    for slice2 in slices:
                        if slice2 == slice1:
                            continue
                        # Extract the device ID
                        deviceid_2 = slice2['deviceid']
                        # Extract the interface name
                        interface_name_2 = slice2['interface_name']
                        if overlay_type == OverlayType.IPv4Overlay:
                            subnets1 = storage_helper.get_ipv4_subnets(
                                deviceid=deviceid_1,
                                tenantid=tenantid,
                                interface_name=interface_name_1
                            )
                            subnets2 = storage_helper.get_ipv4_subnets(
                                deviceid=deviceid_2,
                                tenantid=tenantid,
                                interface_name=interface_name_2
                            )
                            for subnet1 in subnets1:
                                subnet1 = subnet1['subnet']
                                for subnet2 in subnets2:
                                    subnet2 = subnet2['subnet']
                                    if IPv4Network(subnet1).overlaps(
                                        IPv4Network(subnet2)
                                    ):
                                        err = (
                                            'Cannot create overlay: the '
                                            'slices %s and %s have '
                                            'overlapping subnets'
                                            % (slice1, slice2)
                                        )
                                        logging.error(err)
                                        return OverlayServiceReply(
                                            status=Status(
                                                code=STATUS_BAD_REQUEST,
                                                reason=err
                                            )
                                        )
                        elif overlay_type == OverlayType.IPv6Overlay:
                            subnets1 = storage_helper.get_ipv6_subnets(
                                deviceid=deviceid_1,
                                tenantid=tenantid,
                                interface_name=interface_name_1
                            )
                            subnets2 = storage_helper.get_ipv6_subnets(
                                deviceid=deviceid_2,
                                tenantid=tenantid,
                                interface_name=interface_name_2
                            )
                            for subnet1 in subnets1:
                                subnet1 = subnet1['subnet']
                                for subnet2 in subnets2:
                                    subnet2 = subnet2['subnet']
                                    if IPv6Network(subnet1).overlaps(
                                        IPv6Network(subnet2)
                                    ):
                                        err = (
                                            'Cannot create overlay: the '
                                            'slices %s and %s have '
                                            'overlapping subnets'
                                            % (slice1, slice2)
                                        )
                                        logging.error(err)
                                        return OverlayServiceReply(
                                            status=Status(
                                                code=STATUS_BAD_REQUEST,
                                                reason=err
                                            )
                                        )
                can_use_ipv6_addr_for_wan = True
                can_use_ipv4_addr_for_wan = True
                for _slice in slices:
                    # Get WAN interface
                    wan_interface = storage_helper.get_wan_interfaces(
                        deviceid=_slice['deviceid'],
                        tenantid=tenantid
                    )[0]
                    # Check if WAN interface has IPv6 connectivity
                    addrs = storage_helper.get_ext_ipv6_addresses(
                        deviceid=_slice['deviceid'],
                        tenantid=tenantid,
                        interface_name=wan_interface
                    )
                    if addrs is None or len(addrs) == 0:
                        can_use_ipv6_addr_for_wan = False
                    # Check if WAN interface has IPv4 connectivity
                    addrs = storage_helper.get_ext_ipv4_addresses(
                        deviceid=_slice['deviceid'],
                        tenantid=tenantid,
                        interface_name=wan_interface
                    )
                    if addrs is None or len(addrs) == 0:
                        can_use_ipv4_addr_for_wan = False
                if (
                    not can_use_ipv6_addr_for_wan
                    and not can_use_ipv4_addr_for_wan
                ):
                    err = (
                        'Cannot establish a full-mesh between all the WAN '
                        'interfaces'
                    )
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err))
                if tunnel_name == 'SRv6' and not can_use_ipv6_addr_for_wan:
                    err = (
                        'IPv6 transport not available: cannot create a SRv6 '
                        'overlay'
                    )
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err))
                transport_proto = 'ipv4'
                if can_use_ipv6_addr_for_wan:
                    transport_proto = 'ipv6'
                # For SRv6 overlays, Segment Routing transparency must be T0
                # or T1 for each device, otherwise the SRv6 full-mesh overlay
                # cannot be created
                if tunnel_name == 'SRv6':
                    for _slice in slices:
                        incoming_sr_transparency = (
                            storage_helper.get_incoming_sr_transparency(
                                _slice['deviceid'], tenantid
                            )
                        )
                        outgoing_sr_transparency = (
                            storage_helper.get_outgoing_sr_transparency(
                                _slice['deviceid'], tenantid
                            )
                        )
                        # is_ip6tnl_forced = storage_helper.is_ip6tnl_forced(
                        #     _slice['deviceid'], tenantid
                        # )
                        # is_srh_forced = storage_helper.is_srh_forced(
                        #     _slice['deviceid'], tenantid
                        # )
                        if incoming_sr_transparency == 'op':
                            err = (
                                'Device %s has incoming SR Transparency set '
                                'to OP. SRv6 overlays are not supported for '
                                'OP.' % deviceid
                            )
                            logging.error(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                        if outgoing_sr_transparency == 'op':
                            err = (
                                'Device %s has outgoing SR Transparency set '
                                'to OP. SRv6 overlays are not supported for '
                                'OP.' % deviceid
                            )
                            logging.error(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                        # if (
                        #     incoming_sr_transparency == 't1'
                        #     and is_srh_forced
                        # ):
                        #     err = (
                        #         'Device %s has incoming SR Transparency '
                        #         'set to T1 and force-srh set. '
                        #         'Cannot use an SRH for device with incoming '
                        #         'Transparency T1.' % deviceid
                        #     )
                        #     logging.error(err)
                        #     return OverlayServiceReply(
                        #         status=Status(
                        #             code=STATUS_BAD_REQUEST,
                        #             reason=err
                        #         )
                        #     )
                # All the devices must belong to the same tenant
                for device in devices.values():
                    if device['tenantid'] != tenantid:
                        err = (
                            'Error while processing the intent: '
                            'All the devices must belong to the '
                            'same tenant %s' % tenantid
                        )
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                logging.info('All checks passed')
                # All checks passed
                #
                # Save the overlay to the controller state
                overlayid = storage_helper.create_overlay(
                    overlay_name,
                    overlay_type,
                    slices,
                    tenantid,
                    tunnel_name,
                    transport_proto=transport_proto
                )
                if overlayid is None:
                    err = 'Cannot save the overlay to the controller state'
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(
                            code=STATUS_INTERNAL_SERVER_ERROR,
                            reason=err
                        )
                    )
                # Add reverse action to the rollback stack
                rollback.push(
                    func=storage_helper.remove_overlay,
                    overlayid=overlayid,
                    tenantid=tenantid
                )
                # Get tunnel mode
                tunnel_mode = self.tunnel_modes[tunnel_name]
                # Let's create the overlay
                # Create overlay data structure
                status_code = tunnel_mode.init_overlay_data(
                    overlayid, overlay_name, tenantid, tunnel_info
                )
                if status_code != STATUS_OK:
                    err = (
                        'Cannot initialize overlay data '
                        '(overlay %s, tenant %s)'
                        % (overlay_name, tenantid)
                    )
                    logging.warning(err)
                    # # Remove overlay DB status
                    # if storage_helper.remove_overlay(
                    #         overlayid, tenantid) is not True:
                    #     logging.error('Cannot remove overlay. '
                    #                   'Inconsistent data')
                    return OverlayServiceReply(
                        status=Status(code=status_code, reason=err)
                    )
                # Add reverse action to the rollback stack
                rollback.push(
                    func=tunnel_mode.destroy_overlay_data,
                    overlayid=overlayid,
                    overlay_name=overlay_name,
                    tenantid=tenantid,
                    overlay_info=tunnel_info
                )
                # Iterate on slices and add to the overlay
                configured_slices = list()
                for site1 in slices:
                    deviceid = site1['deviceid']
                    interface_name = site1['interface_name']
                    # Init tunnel mode on the devices
                    counter = storage_helper.get_and_inc_tunnel_mode_counter(
                        tunnel_name, deviceid, tenantid
                    )
                    if counter == 0:
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=(
                                storage_helper.dec_and_get_tunnel_mode_counter
                            ),
                            tunnel_name=tunnel_name,
                            deviceid=deviceid,
                            tenantid=tenantid
                        )
                        status_code = tunnel_mode.init_tunnel_mode(
                            deviceid, tenantid, tunnel_info
                        )
                        if status_code != STATUS_OK:
                            err = (
                                'Cannot initialize tunnel mode (device %s '
                                'tenant %s)' % (deviceid, tenantid)
                            )
                            logging.warning(err)
                            # # Remove overlay DB status
                            # if storage_helper.remove_overlay(
                            #         overlayid, tenantid) is not True:
                            #     logging.error(
                            #         'Cannot remove overlay. '
                            #         'Inconsistent data')
                            return OverlayServiceReply(
                                status=Status(code=status_code, reason=err)
                            )
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=tunnel_mode.destroy_tunnel_mode,
                            deviceid=deviceid,
                            tenantid=tenantid,
                            overlay_info=tunnel_info
                        )
                    elif counter is None:
                        err = 'Cannot increase tunnel mode counter'
                        logging.error(err)
                        # # Remove overlay DB status
                        # if storage_helper.remove_overlay(
                        #         overlayid, tenantid) is not True:
                        #     logging.error(
                        #         'Cannot remove overlay. Inconsistent data')
                        return OverlayServiceReply(
                            status=Status(
                                code=STATUS_INTERNAL_SERVER_ERROR,
                                reason=err
                            )
                        )
                    else:
                        # Success
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=(
                                storage_helper.dec_and_get_tunnel_mode_counter
                            ),
                            tunnel_name=tunnel_name,
                            deviceid=deviceid,
                            tenantid=tenantid
                        )
                    # Check if we have already configured the overlay on the
                    # device
                    if deviceid in _devices:
                        # Init overlay on the devices
                        status_code = tunnel_mode.init_overlay(
                            overlayid,
                            overlay_name,
                            overlay_type,
                            tenantid, deviceid,
                            tunnel_info
                        )
                        if status_code != STATUS_OK:
                            err = (
                                'Cannot initialize overlay (overlay %s '
                                'device %s, tenant %s)' %
                                (overlay_name, deviceid, tenantid)
                            )
                            logging.warning(err)
                            # # Remove overlay DB status
                            # if storage_helper.remove_overlay(
                            #         overlayid, tenantid) is not True:
                            #     logging.error(
                            #         'Cannot remove overlay. '
                            #         'Inconsistent data')
                            return OverlayServiceReply(
                                status=Status(code=status_code, reason=err)
                            )
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=tunnel_mode.destroy_overlay,
                            overlayid=overlayid,
                            overlay_name=overlay_name,
                            overlay_type=overlay_type,
                            tenantid=tenantid,
                            deviceid=deviceid,
                            overlay_info=tunnel_info
                        )
                        # Remove device from the to-be-configured devices set
                        _devices.remove(deviceid)
                    # Add the interface to the overlay
                    status_code = tunnel_mode.add_slice_to_overlay(
                        overlayid,
                        overlay_name,
                        deviceid,
                        interface_name,
                        tenantid,
                        tunnel_info
                    )
                    if status_code != STATUS_OK:
                        err = (
                            'Cannot add slice to overlay (overlay %s, '
                            'device %s, slice %s, tenant %s)' %
                            (overlay_name, deviceid, interface_name, tenantid)
                        )
                        logging.warning(err)
                        # # Remove overlay DB status
                        # if storage_helper.remove_overlay(
                        #         overlayid, tenantid) is not True:
                        #     logging.error(
                        #         'Cannot remove overlay. Inconsistent data')
                        return OverlayServiceReply(
                            status=Status(code=status_code, reason=err)
                        )
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=tunnel_mode.remove_slice_from_overlay,
                        overlayid=overlayid,
                        overlay_name=overlay_name,
                        deviceid=deviceid,
                        interface_name=interface_name,
                        tenantid=tenantid,
                        overlay_info=tunnel_info
                    )
                    # Create the tunnel between all the pairs of interfaces
                    for site2 in configured_slices:
                        if site1['deviceid'] != site2['deviceid']:
                            status_code = tunnel_mode.create_tunnel(
                                overlayid,
                                overlay_name,
                                overlay_type,
                                site1, site2,
                                tenantid,
                                tunnel_info
                            )
                            if status_code != STATUS_OK:
                                err = (
                                    'Cannot create tunnel (overlay %s '
                                    'site1 %s site2 %s, tenant %s)'
                                    % (overlay_name, site1, site2, tenantid)
                                )
                                logging.warning(err)
                                # # Remove overlay DB status
                                # if storage_helper.remove_overlay(
                                #         overlayid, tenantid) is not True:
                                #     logging.error(
                                #         'Cannot remove overlay. '
                                #         'Inconsistent data'
                                #     )
                                return OverlayServiceReply(
                                    status=Status(code=status_code, reason=err)
                                )
                            # Add reverse action to the rollback stack
                            rollback.push(
                                func=tunnel_mode.remove_tunnel,
                                overlayid=overlayid,
                                overlay_name=overlay_name,
                                overlay_type=overlay_type,
                                l_slice=site1,
                                r_slice=site2,
                                tenantid=tenantid,
                                overlay_info=tunnel_info
                            )
                    # Add the slice to the configured set
                    configured_slices.append(site1)
            # Success, commit all performed operations
            rollback.commitAll()
        logging.info('All the intents have been processed successfully\n\n')
        # Create the response
        return OverlayServiceReply(
            status=Status(code=STATUS_OK, reason='OK')
        )

    """Remove a VPN"""

    def RemoveOverlay(self, request, context):
        logging.info('RemoveOverlay request received:\n%s', request)
        # Extract the intents from the request message
        for intent in request.intents:
            # Parameters extraction
            #
            # Extract the overlay ID from the intent
            overlayid = intent.overlayid
            # Extract the tenant ID from the intent
            tenantid = intent.tenantid
            # Extract tunnel info
            tunnel_info = intent.tunnel_info
            # Validate the tenant ID
            logging.debug('Validating the tenant ID: %s' % tenantid)
            if not srv6_controller_utils.validate_tenantid(tenantid):
                # If tenant ID is invalid, return an error message
                err = 'Invalid tenant ID: %s' % tenantid
                logging.warning(err)
                return OverlayServiceReply(
                    status=Status(code=STATUS_BAD_REQUEST, reason=err)
                )
            # Check if the tenant is configured
            is_config = storage_helper.is_tenant_configured(
                tenantid
            )
            if is_config is None:
                err = 'Error while checking tenant configuration'
                logging.error(err)
                return TenantReply(
                    status=Status(
                        code=STATUS_INTERNAL_SERVER_ERROR,
                        reason=err
                    )
                )
            elif is_config is False:
                err = (
                    'Cannot remove overlay for a tenant unconfigured'
                    'Tenant not found or error during the '
                    'connection to the db'
                )
                logging.warning(err)
                return TenantReply(
                    status=Status(code=STATUS_BAD_REQUEST, reason=err)
                )
            # Remove VPN
            code, reason = self._RemoveOverlay(
                overlayid, tenantid, tunnel_info
            )
            if code != STATUS_OK:
                return OverlayServiceReply(
                    status=Status(code=code, reason=reason)
                )
        logging.info('All the intents have been processed successfully\n\n')
        # Create the response
        return OverlayServiceReply(
            status=Status(code=STATUS_OK, reason='OK')
        )

    def _RemoveOverlay(self, overlayid, tenantid, tunnel_info):
        with RollbackContext() as rollback:
            # Parameters validation
            #
            # Let's check if the overlay exists
            logging.debug('Checking the overlay: %s' % overlayid)
            overlays = storage_helper.get_overlays(
                overlayids=[overlayid]
            )
            if overlays is None:
                err = 'Error getting the overlay'
                logging.error(err)
                return STATUS_INTERNAL_SERVER_ERROR, err
            elif len(overlays) == 0:
                # If the overlay does not exist, return an error message
                err = 'The overlay %s does not exist' % overlayid
                logging.warning(err)
                return STATUS_BAD_REQUEST, err
            overlay = overlays[0]
            # Check tenant ID
            if tenantid != overlay['tenantid']:
                # If the overlay does not exist, return an error message
                err = (
                    'The overlay %s does not belong to the tenant %s' %
                    (overlayid, tenantid)
                )
                logging.warning(err)
                return STATUS_BAD_REQUEST, err
            # Get the overlay name
            overlay_name = overlay['name']
            # Get the overlay type
            overlay_type = overlay['type']
            # Get the tunnel mode
            tunnel_name = overlay['tunnel_mode']
            tunnel_mode = self.tunnel_modes[tunnel_name]
            # Get the transport proto
            transport_proto = overlay['transport_proto']
            # Get the slices belonging to the overlay
            slices = overlay['slices']
            # All checks passed
            logging.debug('Check passed')
            # Let's remove the VPN
            devices = [slice['deviceid'] for slice in overlay['slices']]
            configured_slices = slices.copy()
            for site1 in slices:
                deviceid = site1['deviceid']
                interface_name = site1['interface_name']
                # Remove the tunnel between all the pairs of interfaces
                for site2 in configured_slices:
                    if site1['deviceid'] != site2['deviceid']:
                        status_code = tunnel_mode.remove_tunnel(
                            overlayid,
                            overlay_name,
                            overlay_type,
                            site1,
                            site2,
                            tenantid,
                            tunnel_info,
                            ignore_errors=True
                        )
                        if status_code != STATUS_OK:
                            err = (
                                'Cannot create tunnel (overlay %s site1 %s '
                                'site2 %s, tenant %s)' %
                                (overlay_name, site1, site2, tenantid)
                            )
                            logging.warning(err)
                            return status_code, err
                # Mark the site1 as unconfigured
                configured_slices.remove(site1)
                # Remove the interface from the overlay
                status_code = tunnel_mode.remove_slice_from_overlay(
                    overlayid,
                    overlay_name,
                    deviceid,
                    interface_name,
                    tenantid,
                    tunnel_info,
                    ignore_errors=True
                )
                if status_code != STATUS_OK:
                    err = (
                        'Cannot remove slice from overlay (overlay %s, '
                        'device %s, slice %s, tenant %s)' %
                        (overlay_name, deviceid, interface_name, tenantid)
                    )
                    logging.warning(err)
                    return status_code, err
                # Check if the overlay and the tunnel mode
                # has already been deleted on the device
                devices.remove(deviceid)
                if deviceid not in devices:
                    # Destroy overlay on the devices
                    status_code = tunnel_mode.destroy_overlay(
                        overlayid,
                        overlay_name,
                        overlay_type,
                        tenantid,
                        deviceid,
                        tunnel_info,
                        ignore_errors=True
                    )
                    if status_code != STATUS_OK:
                        err = (
                            'Cannot destroy overlay (overlay %s, device %s '
                            'tenant %s)' % (overlay_name, deviceid, tenantid)
                        )
                        logging.warning(err)
                        return status_code, err
                # Destroy tunnel mode on the devices
                counter = storage_helper.dec_and_get_tunnel_mode_counter(
                    tunnel_name, deviceid, tenantid
                )
                if counter == 0:
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=storage_helper.get_and_inc_tunnel_mode_counter,
                        tunnel_name=tunnel_name,
                        deviceid=deviceid,
                        tenantid=tenantid
                    )
                    status_code = tunnel_mode.destroy_tunnel_mode(
                        deviceid, tenantid, tunnel_info, ignore_errors=True
                    )
                    if status_code != STATUS_OK:
                        err = (
                            'Cannot destroy tunnel mode (device %s, tenant %s)'
                            % (deviceid, tenantid)
                        )
                        logging.warning(err)
                        return status_code, err
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=tunnel_mode.init_tunnel_mode,
                        deviceid=deviceid,
                        tenantid=tenantid,
                        overlay_info=tunnel_info
                    )
                elif counter is None:
                    err = 'Cannot decrease tunnel mode counter'
                    logging.error(err)
                    return STATUS_INTERNAL_SERVER_ERROR, err
                else:
                    # Success
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=storage_helper.get_and_inc_tunnel_mode_counter,
                        tunnel_name=tunnel_name,
                        deviceid=deviceid,
                        tenantid=tenantid
                    )
            # Destroy overlay data structure
            status_code = tunnel_mode.destroy_overlay_data(
                overlayid,
                overlay_name,
                tenantid,
                tunnel_info,
                ignore_errors=True
            )
            if status_code != STATUS_OK:
                err = (
                    'Cannot destroy overlay data (overlay %s, tenant %s)' %
                    (overlay_name, tenantid)
                )
                logging.warning(err)
                return status_code, err
            # Add reverse action to the rollback stack
            rollback.push(
                func=tunnel_mode.init_overlay_data,
                overlayid=overlayid,
                overlay_name=overlay_name,
                tenantid=tenantid,
                overlay_info=tunnel_info
            )
            # Delete the overlay
            success = storage_helper.remove_overlay(
                overlayid, tenantid
            )
            if success is None or success is False:
                err = 'Cannot remove the overlay from the controller state'
                logging.error(err)
                return STATUS_INTERNAL_SERVER_ERROR, err
            # Add reverse action to the rollback stack
            rollback.push(
                func=storage_helper.create_overlay,
                name=overlay_name,
                type=overlay_type,
                slices=slices,
                tenantid=tenantid,
                tunnel_mode=tunnel_name,
                transport_proto=transport_proto
            )
            # Success, commit all performed operations
            rollback.commitAll()
        # Create the response
        return STATUS_OK, 'OK'

    """Assign an interface to a VPN"""

    def AssignSliceToOverlay(self, request, context):
        logging.info('AssignSliceToOverlay request received:\n%s' % request)
        with RollbackContext() as rollback:
            # Extract the intents from the request message
            for intent in request.intents:
                # Parameters extraction
                #
                # Extract the overlay ID from the intent
                overlayid = intent.overlayid
                # Extract tunnel info
                tunnel_info = intent.tunnel_info
                # Extract tenant ID
                tenantid = intent.tenantid
                # Validate the tenant ID
                logging.debug('Validating the tenant ID: %s' % tenantid)
                if not srv6_controller_utils.validate_tenantid(tenantid):
                    # If tenant ID is invalid, return an error message
                    err = 'Invalid tenant ID: %s' % tenantid
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Check if the tenant is configured
                is_config = storage_helper.is_tenant_configured(
                    tenantid
                )
                if is_config is None:
                    err = 'Error while checking tenant configuration'
                    logging.error(err)
                    return TenantReply(
                        status=Status(
                            code=STATUS_INTERNAL_SERVER_ERROR,
                            reason=err
                        )
                    )
                elif is_config is False:
                    err = (
                        'Cannot update overlay for a tenant unconfigured. '
                        'Tenant not found or error during the '
                        'connection to the db'
                    )
                    logging.warning(err)
                    return TenantReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Get the overlay
                overlays = storage_helper.get_overlays(
                    overlayids=[overlayid]
                )
                if overlays is None:
                    err = 'Error getting the overlay'
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(
                            code=STATUS_INTERNAL_SERVER_ERROR,
                            reason=err
                        )
                    )
                elif len(overlays) == 0:
                    # If the overlay does not exist, return an error message
                    err = 'The overlay %s does not exist' % overlayid
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Take the first overlay
                overlay = overlays[0]
                # Check tenant ID
                if tenantid != overlay['tenantid']:
                    # If the overlay does not exist, return an error message
                    err = (
                        'The overlay %s does not belong to the '
                        'tenant %s' % (overlayid, tenantid)
                    )
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Get the overlay name
                overlay_name = overlay['name']
                # Get the overlay type
                overlay_type = overlay['type']
                # Get the tunnel mode
                tunnel_name = overlay['tunnel_mode']
                tunnel_mode = self.tunnel_modes[tunnel_name]
                # Get the slices belonging to the overlay
                slices = overlay['slices']
                # Get the devices on which the overlay has been configured
                _devices = [_slice['deviceid'] for _slice in slices]
                # Extract the interfaces
                incoming_slices = list()
                incoming_devices = set()
                for _slice in intent.slices:
                    deviceid = _slice.deviceid
                    interface_name = _slice.interface_name
                    # Add the slice to the incoming slices set
                    incoming_slices.append(
                        {
                            'deviceid': deviceid,
                            'interface_name': interface_name
                        }
                    )
                    # Add the device to the incoming devices set
                    # if the overlay has not been initiated on it
                    if deviceid not in _devices:
                        incoming_devices.add(deviceid)
                # Parameters validation
                #
                # Let's check if the overlay exists
                logging.debug('Checking the overlay: %s' % overlay_name)
                # Get the devices
                devices = storage_helper.get_devices(
                    deviceids=list(incoming_devices) + _devices,
                    return_dict=True
                )
                if devices is None:
                    err = 'Error getting devices'
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(
                            code=STATUS_INTERNAL_SERVER_ERROR,
                            reason=err
                        )
                    )
                # Devices validation
                for deviceid in devices:
                    # Let's check if the router exists
                    if deviceid not in devices:
                        # If the device does not exist, return an error
                        # message
                        err = 'Device not found %s' % deviceid
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check if the device is enabled
                    if not devices[deviceid]['enabled']:
                        # If the device is not enabled, return an error
                        # message
                        err = 'The device %s is not enabled' % deviceid
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check if the device is connected
                    if not devices[deviceid]['connected']:
                        # If the device is not connected, return an error
                        # message
                        err = 'The device %s is not connected' % deviceid
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check if the devices have at least a WAN interface
                    wan_found = False
                    for interface in devices[deviceid]['interfaces']:
                        if interface['type'] == InterfaceType.WAN:
                            wan_found = True
                    if not wan_found:
                        # No WAN interfaces found on the device
                        err = (
                            'No WAN interfaces found on the device %s'
                            % deviceid
                        )
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                # Convert interfaces list to a dict representation
                # This step simplifies future processing
                interfaces = dict()
                for deviceid in devices:
                    for interface in devices[deviceid]['interfaces']:
                        interfaces[interface['name']] = interface
                    devices[deviceid]['interfaces'] = interfaces
                # Iterate on the interfaces and extract the
                # interfaces to be assigned
                # to the overlay and validate them
                for _slice in incoming_slices:
                    logging.debug('Validating the slice: %s' % _slice)
                    # A slice is a tuple (deviceid, interface_name)
                    #
                    # Extract the device ID
                    deviceid = _slice['deviceid']
                    # Extract the interface name
                    interface_name = _slice['interface_name']
                    # Let's check if the interface exists
                    if interface_name not in devices[deviceid]['interfaces']:
                        # If the interface does not exists, return an error
                        # message
                        err = 'The interface does not exist'
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check if the interface type is LAN
                    if devices[deviceid]['interfaces'][
                        interface_name
                    ]['type'] != InterfaceType.LAN:
                        # The interface type is not LAN
                        err = (
                            'Cannot add non-LAN interface to the overlay: %s '
                            '(device %s)' % (interface_name, deviceid)
                        )
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check if the slice is already assigned to an overlay
                    _overlay = storage_helper.get_overlay_containing_slice(
                        _slice, tenantid
                    )
                    if _overlay is not None:
                        # Slice already assigned to an overlay
                        err = (
                            'Cannot create overlay: the slice %s is '
                            'already assigned to the overlay %s' %
                            (_slice, _overlay['_id'])
                        )
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check for IP addresses
                    if overlay_type == OverlayType.IPv4Overlay:
                        addrs = storage_helper.get_ipv4_addresses(
                            deviceid=deviceid,
                            tenantid=tenantid,
                            interface_name=interface_name
                        )
                        if len(addrs) == 0:
                            # No IPv4 address assigned to the interface
                            err = (
                                'Cannot create overlay: the slice %s has no '
                                'IPv4 addresses; at least one IPv4 address '
                                'is required to create an IPv4 Overlay'
                                % _slice
                            )
                            logging.error(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                        subnets = storage_helper.get_ipv4_subnets(
                            deviceid=deviceid,
                            tenantid=tenantid,
                            interface_name=interface_name
                        )
                        if len(subnets) == 0:
                            # No IPv4 subnet assigned to the interface
                            err = (
                                'Cannot create overlay: the slice %s has no '
                                'IPv4 subnets; at least one IPv4 subnet is '
                                'required to create an IPv4 Overlay'
                                % _slice
                            )
                            logging.error(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                    elif overlay_type == OverlayType.IPv6Overlay:
                        addrs = storage_helper.get_ipv6_addresses(
                            deviceid=deviceid,
                            tenantid=tenantid,
                            interface_name=interface_name
                        )
                        if len(addrs) == 0:
                            # No IPv6 address assigned to the interface
                            err = (
                                'Cannot create overlay: the slice %s has no '
                                'IPv6 addresses; at least one IPv6 address '
                                'is required to create an IPv6 Overlay'
                                % _slice
                            )
                            logging.error(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                        subnets = storage_helper.get_ipv6_subnets(
                            deviceid=deviceid,
                            tenantid=tenantid,
                            interface_name=interface_name
                        )
                        if len(subnets) == 0:
                            # No IPv6 subnet assigned to the interface
                            err = (
                                'Cannot create overlay: the slice %s has '
                                'no IPv6 subnets; at least one IPv6 subnet '
                                'is required to create an IPv6 Overlay'
                                % _slice
                            )
                            logging.error(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                for slice1 in slices + incoming_slices:
                    # Extract the device ID
                    deviceid_1 = slice1['deviceid']
                    # Extract the interface name
                    interface_name_1 = slice1['interface_name']
                    for slice2 in slices + incoming_slices:
                        if slice2 == slice1:
                            continue
                        # Extract the device ID
                        deviceid_2 = slice2['deviceid']
                        # Extract the interface name
                        interface_name_2 = slice2['interface_name']
                        if overlay_type == OverlayType.IPv4Overlay:
                            subnets1 = storage_helper.get_ipv4_subnets(
                                deviceid=deviceid_1,
                                tenantid=tenantid,
                                interface_name=interface_name_1
                            )
                            subnets2 = storage_helper.get_ipv4_subnets(
                                deviceid=deviceid_2,
                                tenantid=tenantid,
                                interface_name=interface_name_2
                            )
                            for subnet1 in subnets1:
                                subnet1 = subnet1['subnet']
                                for subnet2 in subnets2:
                                    subnet2 = subnet2['subnet']
                                    if IPv4Network(subnet1).overlaps(
                                        IPv4Network(subnet2)
                                    ):
                                        err = (
                                            'Cannot create overlay: the '
                                            'slices %s and %s have '
                                            'overlapping subnets'
                                            % (slice1, slice2)
                                        )
                                        logging.error(err)
                                        return OverlayServiceReply(
                                            status=Status(
                                                code=STATUS_BAD_REQUEST,
                                                reason=err
                                            )
                                        )
                        elif overlay_type == OverlayType.IPv6Overlay:
                            subnets1 = storage_helper.get_ipv6_subnets(
                                deviceid=deviceid_1,
                                tenantid=tenantid,
                                interface_name=interface_name_1
                            )
                            subnets2 = storage_helper.get_ipv6_subnets(
                                deviceid=deviceid_2,
                                tenantid=tenantid,
                                interface_name=interface_name_2
                            )
                            for subnet1 in subnets1:
                                subnet1 = subnet1['subnet']
                                for subnet2 in subnets2:
                                    subnet2 = subnet2['subnet']
                                    if IPv6Network(subnet1).overlaps(
                                        IPv6Network(subnet2)
                                    ):
                                        err = (
                                            'Cannot create overlay: the '
                                            'slices %s and %s have '
                                            'overlapping subnets'
                                            % (slice1, slice2)
                                        )
                                        logging.error(err)
                                        return OverlayServiceReply(
                                            status=Status(
                                                code=STATUS_BAD_REQUEST,
                                                reason=err
                                            )
                                        )
                can_use_ipv6_addr_for_wan = True
                can_use_ipv4_addr_for_wan = True
                for _slice in slices + incoming_slices:
                    # Get WAN interface
                    wan_interface = storage_helper.get_wan_interfaces(
                        deviceid=_slice['deviceid'],
                        tenantid=tenantid
                    )[0]
                    # Check if WAN interface has IPv6 connectivity
                    addrs = storage_helper.get_ext_ipv6_addresses(
                        deviceid=_slice['deviceid'],
                        tenantid=tenantid,
                        interface_name=wan_interface
                    )
                    if addrs is None or len(addrs) == 0:
                        can_use_ipv6_addr_for_wan = False
                    # Check if WAN interface has IPv4 connectivity
                    addrs = storage_helper.get_ext_ipv4_addresses(
                        deviceid=_slice['deviceid'],
                        tenantid=tenantid,
                        interface_name=wan_interface
                    )
                    if addrs is None or len(addrs) == 0:
                        can_use_ipv4_addr_for_wan = False
                if (
                    not can_use_ipv6_addr_for_wan
                    and not can_use_ipv4_addr_for_wan
                ):
                    err = (
                        'Cannot establish a full-mesh between all the WAN '
                        'interfaces'
                    )
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                if tunnel_name == 'SRv6' and not can_use_ipv6_addr_for_wan:
                    err = (
                        'IPv6 transport not available: cannot create a SRv6 '
                        'overlay'
                    )
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # For SRv6 overlays, Segment Routing transparency must be T0
                # or T1 for each device, otherwise the SRv6 full-mesh overlay
                # cannot be created
                if tunnel_name == 'SRv6':
                    for _slice in incoming_slices:
                        incoming_sr_transparency = (
                            storage_helper.get_incoming_sr_transparency(
                                _slice['deviceid'], tenantid
                            )
                        )
                        outgoing_sr_transparency = (
                            storage_helper.get_outgoing_sr_transparency(
                                _slice['deviceid'], tenantid
                            )
                        )
                        # is_ip6tnl_forced = storage_helper.is_ip6tnl_forced(
                        #     _slice['deviceid'], tenantid
                        # )
                        # is_srh_forced = storage_helper.is_srh_forced(
                        #     _slice['deviceid'], tenantid
                        # )
                        if incoming_sr_transparency == 'op':
                            err = (
                                'Device %s has incoming SR Transparency set '
                                'to OP. SRv6 overlays are not supported for '
                                'OP.' % deviceid
                            )
                            logging.error(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                        if outgoing_sr_transparency == 'op':
                            err = (
                                'Device %s has outgoing SR Transparency set '
                                'to OP. SRv6 overlays are not supported for '
                                'OP.' % deviceid
                            )
                            logging.error(err)
                            return OverlayServiceReply(
                                status=Status(
                                    code=STATUS_BAD_REQUEST, reason=err
                                )
                            )
                        # if (
                        #     incoming_sr_transparency == 't1'
                        #     and is_srh_forced
                        # ):
                        #     err = (
                        #         'Device %s has incoming SR Transparency set '
                        #         'to T1 and force-srh set. Cannot use an SRH '
                        #         'for device with incoming Transparency T1.'
                        #         % deviceid
                        #     )
                        #     logging.error(err)
                        #     return OverlayServiceReply(
                        #         status=Status(
                        #             code=STATUS_BAD_REQUEST,
                        #             reason=err
                        #         )
                        #     )
                # All the devices must belong to the same tenant
                for device in devices.values():
                    if device['tenantid'] != tenantid:
                        err = (
                            'Error while processing the intent: '
                            'All the devices must belong to the '
                            'same tenant %s' % tenantid
                        )
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                logging.info('All checks passed')
                # All checks passed
                #
                # Let's assign the interface to the overlay
                configured_slices = slices
                for site1 in incoming_slices:
                    deviceid = site1['deviceid']
                    interface_name = site1['interface_name']
                    # Init tunnel mode on the devices
                    counter = storage_helper.get_and_inc_tunnel_mode_counter(
                        tunnel_name, deviceid, tenantid
                    )
                    if counter == 0:
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=(
                                storage_helper.dec_and_get_tunnel_mode_counter
                            ),
                            tunnel_name=tunnel_name,
                            deviceid=deviceid,
                            tenantid=tenantid
                        )
                        status_code = tunnel_mode.init_tunnel_mode(
                            deviceid, tenantid, tunnel_info
                        )
                        if status_code != STATUS_OK:
                            err = (
                                'Cannot initialize tunnel mode (device %s '
                                'tenant %s)' % (deviceid, tenantid)
                            )
                            logging.warning(err)
                            return OverlayServiceReply(
                                status=Status(code=status_code, reason=err)
                            )
                    elif counter is None:
                        err = 'Cannot increase tunnel mode counter'
                        logging.error(err)
                        return OverlayServiceReply(
                            status=Status(
                                code=STATUS_INTERNAL_SERVER_ERROR,
                                reason=err
                            )
                        )
                    else:
                        # Success
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=(
                                storage_helper.dec_and_get_tunnel_mode_counter
                            ),
                            tunnel_name=tunnel_name,
                            deviceid=deviceid,
                            tenantid=tenantid
                        )
                    # Check if we have already configured the overlay on the
                    # device
                    if deviceid in incoming_devices:
                        # Init overlay on the devices
                        status_code = tunnel_mode.init_overlay(
                            overlayid,
                            overlay_name,
                            overlay_type,
                            tenantid, deviceid,
                            tunnel_info
                        )
                        if status_code != STATUS_OK:
                            err = (
                                'Cannot initialize overlay (overlay %s '
                                'device %s, tenant %s)' %
                                (overlay_name, deviceid, tenantid)
                            )
                            logging.warning(err)
                            return OverlayServiceReply(
                                status=Status(code=status_code, reason=err)
                            )
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=tunnel_mode.destroy_overlay,
                            overlayid=overlayid,
                            overlay_name=overlay_name,
                            overlay_type=overlay_type,
                            tenantid=tenantid,
                            deviceid=deviceid,
                            overlay_info=tunnel_info
                        )
                        # Remove device from the to-be-configured devices set
                        incoming_devices.remove(deviceid)
                    # Add the interface to the overlay
                    status_code = tunnel_mode.add_slice_to_overlay(
                        overlayid,
                        overlay_name,
                        deviceid,
                        interface_name,
                        tenantid,
                        tunnel_info
                    )
                    if status_code != STATUS_OK:
                        err = (
                            'Cannot add slice to overlay (overlay %s, '
                            'device %s, slice %s, tenant %s)' %
                            (overlay_name, deviceid, interface_name, tenantid)
                        )
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=status_code, reason=err)
                        )
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=tunnel_mode.remove_slice_from_overlay,
                        overlayid=overlayid,
                        overlay_name=overlay_name,
                        deviceid=deviceid,
                        interface_name=interface_name,
                        tenantid=tenantid,
                        overlay_info=tunnel_info
                    )
                    # Create the tunnel between all the pairs of interfaces
                    for site2 in configured_slices:
                        if site1['deviceid'] != site2['deviceid']:
                            status_code = tunnel_mode.create_tunnel(
                                overlayid,
                                overlay_name,
                                overlay_type,
                                site1, site2,
                                tenantid,
                                tunnel_info
                            )
                            if status_code != STATUS_OK:
                                err = (
                                    'Cannot create tunnel (overlay %s '
                                    'site1 %s site2 %s, tenant %s)'
                                    % (overlay_name, site1, site2, tenantid)
                                )
                                logging.warning(err)
                                return OverlayServiceReply(
                                    status=Status(code=status_code, reason=err)
                                )
                            # Add reverse action to the rollback stack
                            rollback.push(
                                func=tunnel_mode.remove_tunnel,
                                overlayid=overlayid,
                                overlay_name=overlay_name,
                                overlay_type=overlay_type,
                                l_slice=site1,
                                r_slice=site2,
                                tenantid=tenantid,
                                overlay_info=tunnel_info
                            )
                    # Add the slice to the configured set
                    configured_slices.append(site1)
                # Save the overlay to the state
                success = storage_helper.add_many_slices_to_overlay(
                    overlayid, tenantid, incoming_slices
                )
                if success is None or success is False:
                    err = 'Cannot update overlay in controller state'
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(
                            code=STATUS_INTERNAL_SERVER_ERROR,
                            reason=err
                        )
                    )
                # Add reverse action to the rollback stack
                rollback.push(
                    func=storage_helper.remove_many_slices_from_overlay,
                    overlayid=overlayid,
                    tenantid=tenantid,
                    slices=incoming_slices
                )
            # Success, commit all performed operations
            rollback.commitAll()
        logging.info('All the intents have been processed successfully\n\n')
        # Create the response
        return OverlayServiceReply(
            status=Status(code=STATUS_OK, reason='OK')
        )

    """Remove an interface from a VPN"""

    def RemoveSliceFromOverlay(self, request, context):
        logging.info('RemoveSliceFromOverlay request received:\n%s' % request)
        with RollbackContext() as rollback:
            # Extract the intents from the request message
            for intent in request.intents:
                # Parameters extraction
                #
                # Extract the overlay ID from the intent
                overlayid = intent.overlayid
                # Extract tunnel info
                tunnel_info = intent.tunnel_info
                # Extract tenant ID
                tenantid = intent.tenantid
                # Validate the tenant ID
                logging.debug('Validating the tenant ID: %s' % tenantid)
                if not srv6_controller_utils.validate_tenantid(tenantid):
                    # If tenant ID is invalid, return an error message
                    err = 'Invalid tenant ID: %s' % tenantid
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Check if the tenant is configured
                is_config = storage_helper.is_tenant_configured(
                    tenantid
                )
                if is_config is None:
                    err = 'Error while checking tenant configuration'
                    logging.error(err)
                    return TenantReply(
                        status=Status(
                            code=STATUS_INTERNAL_SERVER_ERROR,
                            reason=err
                        )
                    )
                elif is_config is False:
                    err = (
                        'Cannot update overlay for a tenant unconfigured'
                        'Tenant not found or error during the '
                        'connection to the db'
                    )
                    logging.warning(err)
                    return TenantReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Let's check if the overlay exists
                logging.debug('Checking the overlay: %s' % overlayid)
                overlays = storage_helper.get_overlays(
                    overlayids=[overlayid]
                )
                if overlays is None:
                    err = 'Error getting the overlay'
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(
                            code=STATUS_INTERNAL_SERVER_ERROR,
                            reason=err
                        )
                    )
                elif len(overlays) == 0:
                    # If the overlay does not exist, return an error message
                    err = 'The overlay %s does not exist' % overlayid
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Take the first overlay
                overlay = overlays[0]
                # Check tenant ID
                if tenantid != overlay['tenantid']:
                    # If the overlay does not exist, return an error message
                    err = (
                        'The overlay %s does not belong to the '
                        'tenant %s' % (overlayid, tenantid)
                    )
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
                # Get the overlay name
                overlay_name = overlay['name']
                # Get the overlay type
                overlay_type = overlay['type']
                # Get the tunnel mode
                tunnel_name = overlay['tunnel_mode']
                tunnel_mode = self.tunnel_modes[tunnel_name]
                # Get the slices belonging to the overlay
                slices = overlay['slices']
                # Extract the interfaces
                incoming_slices = list()
                incoming_devices = set()
                for _slice in intent.slices:
                    deviceid = _slice.deviceid
                    interface_name = _slice.interface_name
                    # Add the slice to the incoming slices set
                    incoming_slices.append(
                        {
                            'deviceid': deviceid,
                            'interface_name': interface_name
                        }
                    )
                    # Add the device to the incoming devices set
                    # if the overlay has not been initiated on it
                    if deviceid not in incoming_devices:
                        incoming_devices.add(deviceid)
                # Get the devices
                devices = storage_helper.get_devices(
                    deviceids=incoming_devices, return_dict=True
                )
                if devices is None:
                    err = 'Error getting devices'
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(
                            code=STATUS_INTERNAL_SERVER_ERROR,
                            reason=err
                        )
                    )
                # Convert interfaces list to a dict representation
                # This step simplifies future processing
                interfaces = dict()
                for deviceid in devices:
                    for interface in devices[deviceid]['interfaces']:
                        interfaces[interface['name']] = interface
                    devices[deviceid]['interfaces'] = interfaces
                # Parameters validation
                #
                # Iterate on the interfaces
                # and extract the interfaces to be removed from the VPN
                for _slice in incoming_slices:
                    logging.debug('Validating the slice: %s' % _slice)
                    # A slice is a tuple (deviceid, interface_name)
                    #
                    # Extract the device ID
                    deviceid = _slice['deviceid']
                    # Extract the interface name
                    interface_name = _slice['interface_name']
                    # Let's check if the router exists
                    if deviceid not in devices:
                        # If the device does not exist, return an error
                        # message
                        err = 'Device not found %s' % deviceid
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check if the device is connected
                    if not devices[deviceid]['connected']:
                        # If the device is not connected, return an error
                        # message
                        err = 'The device %s is not connected' % deviceid
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Check if the device is enabled
                    if not devices[deviceid]['enabled']:
                        # If the device is not enabled, return an error message
                        err = 'The device %s is not enabled' % deviceid
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Let's check if the interface exists
                    if interface_name not in devices[deviceid]['interfaces']:
                        # If the interface does not exists, return an error
                        # message
                        err = 'The interface does not exist'
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                    # Let's check if the interface is assigned to the given
                    # overlay
                    if _slice not in overlay['slices']:
                        # The interface is not assigned to the overlay,
                        # return an error message
                        err = (
                            'The interface is not assigned to the overlay %s, '
                            '(name %s, tenantid %s)' %
                            (overlayid, overlay_name, tenantid)
                        )
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                # All the devices must belong to the same tenant
                for device in devices.values():
                    if device['tenantid'] != tenantid:
                        err = (
                            'Error while processing the intent: '
                            'All the devices must belong to the '
                            'same tenant %s' % tenantid
                        )
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=STATUS_BAD_REQUEST, reason=err)
                        )
                logging.debug('All checks passed')
                # All checks passed
                #
                # Let's remove the interface from the VPN
                _devices = [slice['deviceid'] for slice in overlay['slices']]
                configured_slices = slices.copy()
                for site1 in incoming_slices:
                    deviceid = site1['deviceid']
                    interface_name = site1['interface_name']
                    # Remove the tunnel between all the pairs of interfaces
                    for site2 in configured_slices:
                        if site1['deviceid'] != site2['deviceid']:
                            status_code = tunnel_mode.remove_tunnel(
                                overlayid,
                                overlay_name,
                                overlay_type,
                                site1,
                                site2,
                                tenantid,
                                tunnel_info
                            )
                            if status_code != STATUS_OK:
                                err = (
                                    'Cannot create tunnel (overlay %s '
                                    'site1 %s site2 %s, tenant %s)'
                                    % (overlay_name, site1, site2, tenantid)
                                )
                                logging.warning(err)
                                return OverlayServiceReply(
                                    status=Status(code=status_code, reason=err)
                                )
                            # Add reverse action to the rollback stack
                            rollback.push(
                                func=tunnel_mode.create_tunnel,
                                overlayid=overlayid,
                                overlay_name=overlay_name,
                                overlay_type=overlay_type,
                                l_slice=site1,
                                r_slice=site2,
                                tenantid=tenantid,
                                overlay_info=tunnel_info
                            )
                    # Mark the site1 as unconfigured
                    configured_slices.remove(site1)
                    # Remove the interface from the overlay
                    status_code = tunnel_mode.remove_slice_from_overlay(
                        overlayid,
                        overlay_name,
                        deviceid,
                        interface_name,
                        tenantid,
                        tunnel_info
                    )
                    if status_code != STATUS_OK:
                        err = (
                            'Cannot remove slice from overlay (overlay %s, '
                            'device %s, slice %s, tenant %s)' %
                            (overlay_name, deviceid, interface_name, tenantid)
                        )
                        logging.warning(err)
                        return OverlayServiceReply(
                            status=Status(code=status_code, reason=err)
                        )
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=tunnel_mode.add_slice_to_overlay,
                        overlayid=overlayid,
                        overlay_name=overlay_name,
                        deviceid=deviceid,
                        interface_name=interface_name,
                        tenantid=tenantid,
                        overlay_info=tunnel_info
                    )
                    # Check if the overlay and the tunnel mode
                    # has already been deleted on the device
                    _devices.remove(deviceid)
                    if deviceid not in _devices:
                        # Destroy overlay on the devices
                        status_code = tunnel_mode.destroy_overlay(
                            overlayid,
                            overlay_name,
                            overlay_type,
                            tenantid,
                            deviceid,
                            tunnel_info
                        )
                        if status_code != STATUS_OK:
                            err = (
                                'Cannot destroy overlay '
                                '(overlay %s, device %s tenant %s)'
                                % (overlay_name, deviceid, tenantid)
                            )
                            logging.warning(err)
                            return OverlayServiceReply(
                                status=Status(code=status_code, reason=err)
                            )
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=tunnel_mode.init_overlay,
                            overlayid=overlayid,
                            overlay_name=overlay_name,
                            overlay_type=overlay_type,
                            tenantid=tenantid,
                            deviceid=deviceid,
                            overlay_info=tunnel_info
                        )
                    # Destroy tunnel mode on the devices
                    counter = storage_helper.dec_and_get_tunnel_mode_counter(
                        tunnel_name, deviceid, tenantid
                    )
                    if counter == 0:
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=(
                                storage_helper.get_and_inc_tunnel_mode_counter
                            ),
                            tunnel_name=tunnel_name,
                            deviceid=deviceid,
                            tenantid=tenantid
                        )
                        status_code = tunnel_mode.destroy_tunnel_mode(
                            deviceid, tenantid, tunnel_info
                        )
                        if status_code != STATUS_OK:
                            err = (
                                'Cannot destroy tunnel mode (device %s '
                                'tenant %s)' % (deviceid, tenantid)
                            )
                            logging.warning(err)
                            return OverlayServiceReply(
                                status=Status(code=status_code, reason=err)
                            )
                    elif counter is None:
                        err = 'Cannot decrease tunnel mode counter'
                        logging.error(err)
                        return OverlayServiceReply(
                            status=Status(
                                code=STATUS_INTERNAL_SERVER_ERROR,
                                reason=err
                            )
                        )
                    else:
                        # Success
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=(
                                storage_helper.get_and_inc_tunnel_mode_counter
                            ),
                            tunnel_name=tunnel_name,
                            deviceid=deviceid,
                            tenantid=tenantid
                        )
                # Save the overlay to the state
                success = storage_helper.remove_many_slices_from_overlay(
                    overlayid, tenantid, incoming_slices
                )
                if success is None or success is False:
                    err = 'Cannot update overlay in controller state'
                    logging.error(err)
                    return OverlayServiceReply(
                        status=Status(
                            code=STATUS_INTERNAL_SERVER_ERROR,
                            reason=err)
                    )
                # Add reverse action to the rollback stack
                rollback.push(
                    func=storage_helper.add_many_slices_to_overlay,
                    overlayid=overlayid,
                    tenantid=tenantid,
                    slices=incoming_slices
                )
            # Success, commit all performed operations
            rollback.commitAll()
        logging.info('All the intents have been processed successfully\n\n')
        # Create the response
        return OverlayServiceReply(
            status=Status(code=STATUS_OK, reason='OK')
        )

    # Get VPNs from the controller inventory
    def GetOverlays(self, request, context):
        logging.debug('GetOverlays request received')
        # Extract the overlay IDs from the request
        overlayids = list(request.overlayids)
        overlayids = overlayids if len(overlayids) > 0 else None
        # Extract the tenant ID
        tenantid = request.tenantid
        tenantid = tenantid if tenantid != '' else None
        # Parameters validation
        #
        # Validate the overlay IDs
        if overlayids is not None:
            for overlayid in overlayids:
                logging.debug('Validating the overlay ID: %s' % overlayid)
                if not srv6_controller_utils.validate_overlayid(overlayid):
                    # If overlay ID is invalid, return an error message
                    err = 'Invalid overlay ID: %s' % overlayid
                    logging.warning(err)
                    return OverlayServiceReply(
                        status=Status(code=STATUS_BAD_REQUEST, reason=err)
                    )
        # Validate the tenant ID
        if tenantid is not None:
            logging.debug('Validating the tenant ID: %s' % tenantid)
            if not srv6_controller_utils.validate_tenantid(tenantid):
                # If tenant ID is invalid, return an error message
                err = 'Invalid tenant ID: %s' % tenantid
                logging.warning(err)
                return OverlayServiceReply(
                    status=Status(code=STATUS_BAD_REQUEST, reason=err)
                )
        # Create the response
        response = OverlayServiceReply()
        # Build the overlays list
        overlays = storage_helper.get_overlays(
            overlayids=overlayids, tenantid=tenantid
        )
        if overlays is None:
            err = 'Error getting overlays'
            logging.error(err)
            return OverlayServiceReply(
                status=Status(code=STATUS_INTERNAL_SERVER_ERROR, reason=err)
            )
        for _overlay in overlays:
            # Add a new overlay to the overlays list
            overlay = response.overlays.add()
            # Set overlay ID
            overlay.overlayid = str(_overlay['_id'])
            # Set overlay name
            overlay.overlay_name = _overlay['name']
            # Set overlaty type
            overlay.overlay_type = _overlay['type']
            # Set tenant ID
            overlay.tenantid = _overlay['tenantid']
            # Set tunnel mode
            overlay.tunnel_mode = _overlay['tunnel_mode']
            # Set slices
            # Iterate on all slices
            for _slice in _overlay['slices']:
                # Add a new slice to the overlay
                __slice = overlay.slices.add()
                # Add device ID
                __slice.deviceid = _slice['deviceid']
                # Add interface name
                __slice.interface_name = _slice['interface_name']
        # Return the overlays list
        logging.debug('Sending response:\n%s' % response)
        response.status.code = STATUS_OK
        response.status.reason = 'OK'
        return response

    # Get SID lists available between two edge devices
    def GetSIDLists(self, request, context):
        logging.debug('GetSIDLists request received')
        # Extract the ingress and egress device IDs from the request
        ingress_deviceid = request.ingress_deviceid
        ingress_deviceid = ingress_deviceid if ingress_deviceid != '' else None
        egress_deviceid = request.egress_deviceid
        egress_deviceid = egress_deviceid if egress_deviceid != '' else None
        # Extract the tenant ID
        tenantid = request.tenantid
        tenantid = tenantid if tenantid != '' else None
        # Parameters validation
        #
        # Validate the device IDs
        if ingress_deviceid is None:
            err = 'Missing manadtory ingress_deviceid argument'
            logging.error(err)
            return GetSIDListsReply(
                status=Status(code=STATUS_BAD_REQUEST, reason=err)
            )
        if egress_deviceid is None:
            err = 'Missing manadtory egress_deviceid argument'
            logging.error(err)
            return GetSIDListsReply(
                status=Status(code=STATUS_BAD_REQUEST, reason=err)
            )
        # Validate the tenant ID
        if tenantid is not None:
            logging.debug('Validating the tenant ID: %s' % tenantid)
            if not srv6_controller_utils.validate_tenantid(tenantid):
                # If tenant ID is invalid, return an error message
                err = 'Invalid tenant ID: %s' % tenantid
                logging.warning(err)
                return GetSIDListsReply(
                    status=Status(code=STATUS_BAD_REQUEST, reason=err)
                )
        # Create the response
        response = GetSIDListsReply()
        # Get the SID list (in both the directions) between the two devices
        # for each overlay
        status, err, sid_lists = self.tunnel_modes['SRv6'].get_sid_lists(
            ingress_deviceid=ingress_deviceid,
            egress_deviceid=egress_deviceid,
            tenantid=tenantid
        )
        if status != NbStatusCode.STATUS_OK:
            logging.error(err)
            return GetSIDListsReply(
                status=Status(code=STATUS_INTERNAL_SERVER_ERROR, reason=err)
            )
        for _sid_list in sid_lists:
            # Retrieve the SID list
            sid_list = response.sid_lists.add()
            sid_list.overlayid = _sid_list['overlayid']
            sid_list.overlay_name = _sid_list['overlay_name']
            sid_list.tenantid = _sid_list['tenantid']
            sid_list.direct_sid_list.extend(_sid_list['direct_sid_list'])
            sid_list.return_sid_list.extend(_sid_list['return_sid_list'])
        # Return the overlays list
        logging.debug('Sending response:\n%s' % response)
        response.status.code = STATUS_OK
        response.status.reason = 'OK'
        return response

    def prepare_db_for_device_reconciliation(self, deviceid, tenantid):
        # self.stamp_controller.storage.set_sender_inizialized(
        #    node_id=deviceid, tenantid=tenantid, is_initialized=False)
        storage_helper.reset_overlay_stats(
            deviceid=deviceid, tenantid=tenantid
        )
        for tunnel_name in self.tunnel_modes:
            storage_helper.reset_tunnel_mode_counter(
                tunnel_name=tunnel_name, deviceid=deviceid, tenantid=tenantid
            )
        storage_helper.reset_created_tunnels(
            deviceid=deviceid, tenantid=tenantid
        )
        if self.stamp_controller is not None:
            if self.stamp_controller.storage.get_stamp_node(
                    node_id=deviceid, tenantid=tenantid
            ) is not None:
                self.stamp_controller.storage.set_sender_inizialized(
                    node_id=deviceid, tenantid=tenantid, is_initialized=False
                )
                self.stamp_controller.storage.set_reflector_inizialized(
                    node_id=deviceid, tenantid=tenantid, is_initialized=False
                )
        return STATUS_OK

    def device_reconciliation(self, deviceid, tenantid):
        logging.debug('Device Reconcliation started')
        err = STATUS_OK
        # Get the device
        device = storage_helper.get_device(
            deviceid=deviceid, tenantid=tenantid
        )
        if device is None:
            logging.error('Error getting device')
            return status_codes_pb2.STATUS_INTERNAL_ERROR
        if not device['configured']:
            logging.warning('Device not yet configured. Nothing to reconcile')
            return err
        default_interfaces = dict()
        for interface in device['default']['interfaces']:
            default_interfaces[interface['name']] = dict()
            default_interfaces[
                interface['name']
            ]['ipv4_addrs'] = interface['ipv4_addrs']
            default_interfaces[
                interface['name']
            ]['ipv6_addrs'] = interface['ipv6_addrs']
        for interface in device['interfaces']:
            if interface['type'] == InterfaceType.WAN or \
                    interface['type'] == InterfaceType.UNKNOWN:
                logging.warning(
                    'Cannot set IP address of WAN interface. Skipping'
                )
                continue
            if len(interface['ipv4_addrs']) > 0:
                addrs = list()
                for addr in default_interfaces[
                    interface['name']
                ]['ipv4_addrs']:
                    addrs.append(addr)
                response = self.srv6_manager.remove_many_ipaddr(
                    device['mgmtip'],
                    self.grpc_client_port,
                    addrs=addrs,
                    device=interface['name'],
                    family=AF_UNSPEC
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    # If the operation has failed,
                    # report an error message
                    logging.warning(
                        'Cannot remove the public addresses '
                        'from the interface'
                    )
                    err = status_codes_pb2.STATUS_INTERNAL_ERROR
                # Add IP address to the interface
                for ipv4_addr in interface['ipv4_addrs']:
                    response = self.srv6_manager.create_ipaddr(
                        device['mgmtip'],
                        self.grpc_client_port, ip_addr=ipv4_addr,
                        device=interface['name'], family=AF_INET,
                        ignore_errors=True
                    )
                    if response == SbStatusCode.STATUS_FILE_EXISTS:
                        logging.warning(
                            'The IPv4 address already exists. Skipping'
                        )
                    elif response != SbStatusCode.STATUS_SUCCESS:
                        # If the operation has failed,
                        # report an error message
                        logging.warning(
                            'Cannot assign the private VPN IP address '
                            'to the interface'
                        )
                        err = status_codes_pb2.STATUS_INTERNAL_ERROR
            if len(interface['ipv6_addrs']) > 0:
                addrs = list()
                nets = list()
                for addr in default_interfaces[
                    interface['name']
                ]['ipv6_addrs']:
                    addrs.append(addr)
                    nets.append(str(IPv6Interface(addr).network))
                response = self.srv6_manager.remove_many_ipaddr(
                    device['mgmtip'],
                    self.grpc_client_port,
                    addrs=addrs,
                    nets=nets,
                    device=interface['name'],
                    family=AF_UNSPEC
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    # If the operation has failed,
                    # report an error message
                    logging.warning(
                        'Cannot remove the public addresses '
                        'from the interface'
                    )
                    err = status_codes_pb2.STATUS_INTERNAL_ERROR
                # Add IP address to the interface
                for ipv6_addr in interface['ipv6_addrs']:
                    net = IPv6Interface(ipv6_addr).network.__str__()
                    response = self.srv6_manager.create_ipaddr(
                        device['mgmtip'],
                        self.grpc_client_port,
                        ip_addr=ipv6_addr,
                        device=interface['name'],
                        net=net,
                        family=AF_INET6,
                        ignore_errors=True
                    )
                    if response == SbStatusCode.STATUS_FILE_EXISTS:
                        logging.warning(
                            'The IPv4 address already exists. Skipping'
                        )
                    elif response != SbStatusCode.STATUS_SUCCESS:
                        # If the operation has failed,
                        # report an error message
                        logging.warning(
                            'Cannot assign the private VPN IP address '
                            'to the interface'
                        )
                        err = status_codes_pb2.STATUS_INTERNAL_ERROR
            # Push the new configuration
            if err == STATUS_OK:
                logging.debug(
                    'The device %s has been configured successfully' % deviceid
                )
            else:
                err = 'The device %s rejected the configuration' % deviceid
                logging.error(err)
                return STATUS_BAD_REQUEST
        logging.info('The device configuration has been saved\n\n')
        # Setup STAMP information
        if ENABLE_STAMP_SUPPORT:
            logging.info('Configuring STAMP information\n\n')
            # Lookup the WAN interfaces
            # TODO currently we only support a single WAN interface,
            # so we look for the address of the first WAN interface
            # In the future we should support multiple interfaces
            wan_ip = None
            wan_ifaces = None
            for interface in device['interfaces']:
                if interface['type'] == InterfaceType.WAN and \
                        len(interface['ipv6_addrs']) > 0:
                    wan_ip = interface['ipv6_addrs'][0].split('/')[0]
                    wan_ifaces = [interface['name']]
                    break
            # Configure information
            if self.stamp_controller.storage.get_stamp_node(
                    node_id=device['deviceid'],
                    tenantid=tenantid
            ) is None:
                self.stamp_controller.add_stamp_node(
                    node_id=device['deviceid'],
                    node_name=device['name'],
                    grpc_ip=device['mgmtip'],
                    grpc_port=self.grpc_client_port,
                    ip=wan_ip,
                    sender_port=42069,
                    reflector_port=862,
                    interfaces=wan_ifaces,
                    stamp_source_ipv6_address=wan_ip,
                    is_sender=True,
                    is_reflector=True,
                    initialize=False,
                    tenantid=tenantid
                )
            # Configure information
            self.stamp_controller.init_stamp_node(
                node_id=device['deviceid'], tenantid=tenantid
            )

            stamp_sessions = self.stamp_controller.storage.get_stamp_sessions(
                tenantid=tenantid
            )
            for session in stamp_sessions:
                if session.sender.node_id == deviceid:
                    self.stamp_controller.storage.set_session_running(
                        ssid=session.ssid, tenantid=tenantid, is_running=False
                    )
                    self.stamp_controller._create_stamp_sender_session(
                        ssid=session.ssid,
                        sender=session.sender,
                        reflector=session.reflector,
                        sidlist=session.sidlist,
                        interval=session.interval,
                        auth_mode=session.auth_mode,
                        key_chain=session.sender_key_chain,
                        timestamp_format=session.sender_timestamp_format,
                        packet_loss_type=session.packet_loss_type,
                        delay_measurement_mode=session.delay_measurement_mode
                    )
                if session.reflector.node_id == deviceid:
                    self.stamp_controller.storage.set_session_running(
                        ssid=session.ssid, tenantid=tenantid, is_running=False
                    )
                    self.stamp_controller._create_stamp_reflector_session(
                        ssid=session.ssid,
                        sender=session.sender,
                        reflector=session.reflector,
                        return_sidlist=session.return_sidlist,
                        auth_mode=session.auth_mode,
                        key_chain=session.reflector_key_chain,
                        timestamp_format=session.reflector_timestamp_format,
                        session_reflector_mode=session.session_reflector_mode
                    )
        logging.debug('Device Reconcliation completed')
        # Create the response
        return STATUS_OK

    def overlay_reconciliation(self, deviceid, tenantid):
        logging.info(
            'Overlay Reconcliation started: deviceid %s, tenantid %s',
            deviceid,
            tenantid
        )
        overlays = storage_helper.get_overlays_containing_device(
            deviceid=deviceid, tenantid=tenantid
        )
        for overlay in overlays:
            overlayid = str(overlay['_id'])
            overlay_name = overlay['name']
            tenantid = overlay['tenantid']
            overlay_type = overlay['type']
            tunnel_name = overlay['tunnel_mode']
            slices = overlay['slices']
            tunnel_info = None
            # Get tunnel mode
            tunnel_mode = self.tunnel_modes[tunnel_name]
            # Let's create the overlay
            # Create overlay data structure
            status_code = tunnel_mode.init_overlay_data_reconciliation(
                overlayid=overlayid,
                overlay_name=overlay_name,
                tenantid=tenantid,
                overlay_info=tunnel_info
            )
            if status_code != STATUS_OK:
                err = (
                    'Cannot initialize overlay data (overlay %s, tenant %s)' %
                    (overlay_name, tenantid)
                )
                logging.warning(err)
                return
            # Iterate on slices and add to the overlay
            configured_slices = list()
            for site1 in slices:
                _deviceid = site1['deviceid']
                interface_name = site1['interface_name']
                # Init tunnel mode on the
                if deviceid == _deviceid:
                    counter = storage_helper.get_and_inc_tunnel_mode_counter(
                        tunnel_name, deviceid, tenantid
                    )
                    if counter == 0:
                        status_code = (
                            tunnel_mode.init_tunnel_mode_reconciliation(
                                deviceid, tenantid, tunnel_info
                            )
                        )
                        if status_code != STATUS_OK:
                            err = (
                                'Cannot initialize tunnel mode (device %s '
                                'tenant %s)' % (deviceid, tenantid)
                            )
                            logging.warning(err)
                            return
                    elif counter is None:
                        err = 'Cannot increase tunnel mode counter'
                        logging.error(err)
                        return
                # Init overlay on the devices
                if deviceid == _deviceid:
                    status_code = tunnel_mode.init_overlay_reconciliation(
                        overlayid,
                        overlay_name,
                        overlay_type,
                        tenantid, deviceid,
                        tunnel_info
                    )
                    if status_code != STATUS_OK:
                        err = (
                            'Cannot initialize overlay (overlay %s '
                            'device %s, tenant %s)' %
                            (overlay_name, deviceid, tenantid)
                        )
                        logging.warning(err)
                        return
                # Add the interface to the overlay
                if deviceid == _deviceid:
                    status_code = (
                        tunnel_mode.add_slice_to_overlay_reconciliation(
                            overlayid,
                            overlay_name,
                            deviceid,
                            interface_name,
                            tenantid,
                            tunnel_info
                        )
                    )
                    if status_code != STATUS_OK:
                        err = (
                            'Cannot add slice to overlay (overlay %s, '
                            'device %s, slice %s, tenant %s)' %
                            (overlay_name, deviceid, interface_name, tenantid)
                        )
                        logging.warning(err)
                        return
                # Create the tunnel between all the pairs of interfaces
                for site2 in configured_slices:
                    if site1['deviceid'] != site2['deviceid']:
                        if site1['deviceid'] == deviceid:
                            status_code = (
                                tunnel_mode.create_tunnel_reconciliation_l(
                                    overlayid,
                                    overlay_name,
                                    overlay_type,
                                    site1, site2,
                                    tenantid,
                                    tunnel_info
                                )
                            )
                            if status_code != STATUS_OK:
                                err = (
                                    'Cannot create tunnel (overlay %s '
                                    'site1 %s site2 %s, tenant %s)'
                                    % (overlay_name, site1, site2, tenantid)
                                )
                                logging.warning(err)
                                return
                            status_code = (
                                tunnel_mode.create_tunnel_reconciliation_r(
                                    overlayid,
                                    overlay_name,
                                    overlay_type,
                                    site2, site1,
                                    tenantid,
                                    tunnel_info
                                )
                            )
                            if status_code != STATUS_OK:
                                err = (
                                    'Cannot create tunnel (overlay %s '
                                    'site1 %s site2 %s, tenant %s)'
                                    % (overlay_name, site1, site2, tenantid)
                                )
                                logging.warning(err)
                                return
                        if site2['deviceid'] == deviceid:
                            status_code = (
                                tunnel_mode.create_tunnel_reconciliation_l(
                                    overlayid,
                                    overlay_name,
                                    overlay_type,
                                    site2, site1,
                                    tenantid,
                                    tunnel_info
                                )
                            )
                            if status_code != STATUS_OK:
                                err = (
                                    'Cannot create tunnel (overlay %s '
                                    'site1 %s site2 %s, tenant %s)'
                                    % (overlay_name, site1, site2, tenantid)
                                )
                                logging.warning(err)
                                return
                            status_code = (
                                tunnel_mode.create_tunnel_reconciliation_r(
                                    overlayid,
                                    overlay_name,
                                    overlay_type,
                                    site1,
                                    site2,
                                    tenantid,
                                    tunnel_info
                                )
                            )
                            if status_code != STATUS_OK:
                                err = (
                                    'Cannot create tunnel (overlay %s '
                                    'site1 %s site2 %s, tenant %s)'
                                    % (overlay_name, site1, site2, tenantid)
                                )
                                logging.warning(err)
                                return
                # Add the slice to the configured set
                configured_slices.append(site1)
                logging.info(
                    'Reconciliation of overlays completed successfully\n\n'
                )
        logging.debug(
            'Overlay Reconcliation completed: deviceid %s, tenantid %s',
            deviceid,
            tenantid
        )
        # Create the response
        return STATUS_OK


def create_server(grpc_server_ip=DEFAULT_GRPC_SERVER_IP,
                  grpc_server_port=DEFAULT_GRPC_SERVER_PORT,
                  grpc_client_port=DEFAULT_GRPC_CLIENT_PORT,
                  nb_secure=DEFAULT_SECURE, server_key=DEFAULT_KEY,
                  server_certificate=DEFAULT_CERTIFICATE,
                  sb_secure=DEFAULT_SECURE,
                  client_certificate=DEFAULT_CERTIFICATE,
                  southbound_interface=DEFAULT_SB_INTERFACE,
                  topo_graph=None, vpn_dict=None,
                  devices=None,
                  vpn_file=DEFAULT_VPN_DUMP,
                  controller_state=None,
                  verbose=DEFAULT_VERBOSE):
    # Initialize controller state
    # controller_state = srv6_controller_utils.ControllerState(
    #    topology=topo_graph,
    #    devices=devices,
    #    vpn_dict=vpn_dict,
    #    vpn_file=vpn_file
    # )
    # Create SRv6 Manager
    srv6_manager = sb_grpc_client.SRv6Manager(
        secure=sb_secure, certificate=client_certificate
    )
    # Setup gRPC server
    #
    # Create the server and add the handler
    grpc_server = grpc.server(futures.ThreadPoolExecutor())
    # Add the STAMP controller
    stamp_controller = None
    if ENABLE_STAMP_SUPPORT:
        mongodb_client = storage_helper.get_mongodb_session()
        stamp_controller = stamp_controller_module.run_grpc_server(
            server=grpc_server,
            storage='mongodb',
            mongodb_client=mongodb_client
        )
    # Initialize the Northbound Interface
    service = NorthboundInterface(
        grpc_client_port,
        srv6_manager,
        southbound_interface,
        verbose,
        stamp_controller
    )
    srv6_vpn_pb2_grpc.add_NorthboundInterfaceServicer_to_server(
        service, grpc_server
    )
    # If secure mode is enabled, we need to create a secure endpoint
    if nb_secure:
        # Read key and certificate
        with open(server_key, 'rb') as f:
            key = f.read()
        with open(server_certificate, 'rb') as f:
            certificate = f.read()
        # Create server SSL credentials
        grpc_server_credentials = grpc.ssl_server_credentials(
            ((key, certificate,),)
        )
        # Create a secure endpoint
        grpc_server.add_secure_port(
            '[%s]:%s' % (grpc_server_ip, grpc_server_port),
            grpc_server_credentials
        )
    else:
        # Create an insecure endpoint
        grpc_server.add_insecure_port(
            '[%s]:%s' % (grpc_server_ip, grpc_server_port)
        )
    return grpc_server, service


# Start gRPC server
def start_server(grpc_server_ip=DEFAULT_GRPC_SERVER_IP,
                 grpc_server_port=DEFAULT_GRPC_SERVER_PORT,
                 grpc_client_port=DEFAULT_GRPC_CLIENT_PORT,
                 nb_secure=DEFAULT_SECURE, server_key=DEFAULT_KEY,
                 server_certificate=DEFAULT_CERTIFICATE,
                 sb_secure=DEFAULT_SECURE,
                 client_certificate=DEFAULT_CERTIFICATE,
                 southbound_interface=DEFAULT_SB_INTERFACE,
                 topo_graph=None, vpn_dict=None,
                 devices=None,
                 vpn_file=DEFAULT_VPN_DUMP,
                 controller_state=None,
                 verbose=DEFAULT_VERBOSE):
    # Create the gRPC server
    grpc_server, _ = create_server(
        grpc_server_ip=grpc_server_ip,
        grpc_server_port=grpc_server_port,
        grpc_client_port=grpc_client_port,
        nb_secure=nb_secure, server_key=server_key,
        server_certificate=server_certificate,
        sb_secure=sb_secure,
        client_certificate=client_certificate,
        southbound_interface=southbound_interface,
        topo_graph=topo_graph, vpn_dict=vpn_dict,
        devices=devices,
        vpn_file=vpn_file,
        controller_state=controller_state,
        verbose=verbose
    )
    # Start the loop for gRPC
    logging.info('Listening gRPC')
    grpc_server.start()
    while True:
        time.sleep(5)


# Parse arguments
def parse_arguments():
    # Get parser
    parser = ArgumentParser(
        description='gRPC-based Northbound APIs for SRv6 Controller'
    )
    # Debug logs
    parser.add_argument(
        '-d', '--debug', action='store_true', help='Activate debug logs'
    )
    # gRPC secure mode
    parser.add_argument(
        '-s',
        '--secure',
        action='store_true',
        default=DEFAULT_SECURE,
        help='Activate secure mode'
    )
    # Verbose mode
    parser.add_argument(
        '-v',
        '--verbose',
        action='store_true',
        dest='verbose',
        default=DEFAULT_VERBOSE,
        help='Enable verbose mode'
    )
    # Path of intput topology file
    parser.add_argument(
        '-t',
        '--topo-file',
        dest='topo_file',
        action='store',
        required=True,
        default=DEFAULT_TOPOLOGY_FILE,
        help='Filename of the exported topology'
    )
    # Path of output VPN file
    parser.add_argument(
        '-f',
        '--vpn-file',
        dest='vpn_dump',
        action='store',
        default=None,
        help='Filename of the VPN dump'
    )
    # Server certificate file
    parser.add_argument(
        '-c',
        '--certificate',
        store='certificate',
        action='store',
        default=DEFAULT_CERTIFICATE,
        help='Server certificate file'
    )
    # Server key
    parser.add_argument(
        '-k',
        '--key',
        store='key',
        action='store',
        default=DEFAULT_KEY,
        help='Server key file'
    )
    # IP address of the gRPC server
    parser.add_argument(
        '-i',
        '--ip',
        store='grpc_server_ip',
        action='store',
        default=DEFAULT_GRPC_SERVER_IP,
        help='IP address of the gRPC server'
    )
    # Port of the gRPC server
    parser.add_argument(
        '-p',
        '--server-port',
        store='grpc_server_port',
        action='store',
        default=DEFAULT_GRPC_SERVER_PORT,
        help='Port of the gRPC server'
    )
    # Port of the gRPC client
    parser.add_argument(
        '-o',
        '--client-port',
        store='grpc_client_port',
        action='store',
        default=DEFAULT_GRPC_CLIENT_PORT,
        help='Port of the gRPC client'
    )
    # Southbound interface
    parser.add_argument(
        '-b',
        '--southbound',
        action='store',
        dest='southbound_interface',
        default=DEFAULT_SB_INTERFACE,
        help='Southbound interface\nSupported interfaces: [grpc]'
    )
    # Parse input parameters
    args = parser.parse_args()
    # Done, return
    return args


if __name__ == '__main__':
    # Parse options
    args = parse_arguments()
    # Setup properly the logger
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger().setLevel(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
        logging.getLogger().setLevel(level=logging.INFO)
    # Debug settings
    SERVER_DEBUG = logging.getEffectiveLevel() == logging.DEBUG
    logging.info('SERVER_DEBUG:' + str(SERVER_DEBUG))
    # Input topology file
    topo_file = args.topo_file
    # Output VPN file
    vpn_dump = args.vpn_dump
    # Setup properly the secure mode
    if args.secure:
        secure = True
    else:
        secure = False
    # Server certificate file
    certificate = args.certificate
    # Server key
    key = args.key
    # IP of the gRPC server
    grpc_server_ip = args.grpc_server_ip
    # Port of the gRPC server
    grpc_server_port = args.grpc_server_port
    # Port of the gRPC client
    grpc_client_port = args.grpc_client_port
    # Southbound interface
    southbound_interface = args.southbound_interface
    # Setup properly the verbose mode
    if args.verbose:
        verbose = True
    else:
        verbose = False
    # Check southbound interface
    if southbound_interface not in SUPPORTED_SB_INTERFACES:
        # The southbound interface is invalid or not supported
        logging.warning(
            'Error: The %s interface is invalid or not yet supported\n'
            'Supported southbound interfaces: %s' % SUPPORTED_SB_INTERFACES
        )
        sys.exit(-2)
    # Wait until topology json file is ready
    while True:
        if os.path.isfile(topo_file):
            # The file is ready, we are ready to start server
            break
        # The file is not ready, wait for INTERVAL_CHECK_FILES seconds before
        # retrying
        print('Waiting for TOPOLOGY_FILE...')
        time.sleep(INTERVAL_CHECK_FILES)
    # Update the topology
    topo_graph = srv6_controller_utils.load_topology_from_json_dump(topo_file)
    if topo_graph is not None:
        # Start server
        start_server(
            grpc_server_ip, grpc_server_port, grpc_client_port, secure, key,
            certificate, southbound_interface, topo_graph, None, vpn_dump,
            verbose
        )
        while True:
            time.sleep(5)
    else:
        print('Invalid topology')
