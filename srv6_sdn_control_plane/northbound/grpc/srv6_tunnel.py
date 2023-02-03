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
import logging
from socket import AF_INET
from socket import AF_INET6
# SRv6 dependencies
from srv6_sdn_control_plane.northbound.grpc import tunnel_mode
from srv6_sdn_control_plane.northbound.grpc import srv6_tunnel_utils
from srv6_sdn_control_plane.southbound.grpc import sb_grpc_client
from srv6_sdn_control_plane import srv6_controller_utils
from srv6_sdn_control_plane.srv6_controller_utils import OverlayType
from srv6_sdn_proto import status_codes_pb2
from srv6_sdn_proto.status_codes_pb2 import NbStatusCode, SbStatusCode
from srv6_sdn_controller_state import (
    srv6_sdn_controller_state as storage_helper
)
from srv6_sdn_controller_state.srv6_sdn_controller_state import DeviceState

from rollbackcontext import RollbackContext

# Global variables definition

# Default gRPC client port
DEFAULT_GRPC_CLIENT_PORT = 12345
# Verbose mode
DEFAULT_VERBOSE = False
# Logger reference
logger = logging.getLogger(__name__)


class SRv6Tunnel(tunnel_mode.TunnelMode):
    """gRPC request handler"""

    def __init__(
        self,
        grpc_client_port=DEFAULT_GRPC_CLIENT_PORT,
        controller_state=None,
        verbose=DEFAULT_VERBOSE
    ):
        # Name of the tunnel mode
        self.name = 'SRv6'
        # Port of the gRPC client
        self.grpc_client_port = grpc_client_port
        # Verbose mode
        self.verbose = verbose
        # VPN dict
        self.vpn_dict = None
        # Create SRv6 Manager
        self.srv6_manager = sb_grpc_client.SRv6Manager()
        # Initialize controller state
        self.controller_state_srv6 = \
            srv6_tunnel_utils.ControllerStateSRv6(controller_state)

    def exec_or_mark_device_inconsitent(
        self,
        rollback_func,
        deviceid,
        tenantid,
        *args,
        **kwargs
    ):
        try:
            if rollback_func(*args, **kwargs) != SbStatusCode.STATUS_SUCCESS:
                # Change device state to reboot required
                success = storage_helper.change_device_state(
                    deviceid=deviceid,
                    tenantid=tenantid,
                    new_state=DeviceState.REBOOT_REQUIRED
                )
                if success is False or success is None:
                    logging.error('Error changing the device state')
                    return status_codes_pb2.STATUS_INTERNAL_ERROR
        except Exception:
            # Change device state to reboot required
            success = storage_helper.change_device_state(
                deviceid=deviceid,
                tenantid=tenantid,
                new_state=DeviceState.REBOOT_REQUIRED
            )
            if success is False or success is None:
                logging.error('Error changing the device state')
                return status_codes_pb2.STATUS_INTERNAL_ERROR

    def _create_tunnel_uni(
        self,
        overlayid,
        overlay_name,
        overlay_type,
        l_slice,
        r_slice,
        tenantid,
        overlay_info
    ):
        logger.debug(
            'Attempting to create unidirectional tunnel from %s to %s',
            l_slice['interface_name'],
            r_slice['interface_name']
        )
        with RollbackContext() as rollback:
            # Check if the unidirectional tunnel
            # between the two slices already exists
            #
            # Increase the number of tunnels
            num_tunnels = (
                storage_helper.inc_and_get_tunnels_counter(
                    overlayid, tenantid, l_slice['deviceid'], r_slice
                )
            )
            # Add reverse action to the rollback stack
            rollback.push(
                func=storage_helper.dec_and_get_tunnels_counter,
                overlayid=overlayid,
                tenantid=tenantid,
                deviceid=l_slice['deviceid'],
                dest_slice=r_slice
            )
            # If the uni tunnel already exists, we have done
            if num_tunnels is not None and num_tunnels > 1:
                logger.debug(
                    'Skip tunnel %s %s',
                    l_slice['interface_name'],
                    r_slice['interface_name']
                )
                # Success, commit all performed operations
                rollback.commitAll()
                return NbStatusCode.STATUS_OK
            # Configure the tunnel
            #
            # Get router address
            l_deviceip = storage_helper.get_router_mgmtip(
                l_slice['deviceid'], tenantid
            )
            if l_deviceip is None:
                # Cannot get the router address
                logger.warning('Cannot get the router address')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get router address
            r_deviceip = storage_helper.get_router_mgmtip(
                r_slice['deviceid'], tenantid
            )
            if r_deviceip is None:
                # Cannot get the router address
                logger.warning('Cannot get the router address')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Any non-loopback device
            # We use the WAN interface
            # in order to solve an issue of routes getting deleted when the
            # interface is assigned to a VRF
            dev = storage_helper.get_wan_interfaces(
                l_slice['deviceid'], tenantid
            )
            if dev is None:
                # Cannot get wan interface
                logger.warning('Cannot get WAN interface')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            if len(dev) == 0:
                # Cannot get wan interface
                logger.warning('Cannot get WAN interface. No WAN interfaces')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            dev = dev[0]
            # Get the table ID
            tableid = storage_helper.get_tableid(
                overlayid, tenantid
            )
            if tableid is None:
                logger.warning('Cannot retrieve VPN table ID')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get the SID
            sid_list = self.controller_state_srv6.get_sid_list(
                r_slice['deviceid'], tenantid, tableid
            )
            # pyroute2 requires SID list in reverse order
            sid_list = sid_list[::-1]
            # Get the subnets
            if overlay_type == OverlayType.IPv6Overlay:
                subnets = storage_helper.get_ipv6_subnets(
                    r_slice['deviceid'], tenantid, r_slice['interface_name']
                )
            elif overlay_type == OverlayType.IPv4Overlay:
                subnets = storage_helper.get_ipv4_subnets(
                    r_slice['deviceid'], tenantid, r_slice['interface_name']
                )
            else:
                logger.warning(
                    'Error: Unsupported VPN type: %s', overlay_type
                )
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Check if a tunnel between the devices already exists
            first_tunnel = True
            _tunnels = storage_helper.get_tunnel(
                overlayid=overlayid,
                ldeviceid=l_slice['deviceid'],
                rdeviceid=r_slice['deviceid'],
                tenantid=tenantid
            )
            if _tunnels is not None:
                first_tunnel = False
            # Add the tunnel to the controller state and get the tunnel ID of
            # the new tunnel
            tunnel = storage_helper.add_tunnel_to_overlay(
                overlayid=overlayid,
                ldeviceid=l_slice['deviceid'],
                rdeviceid=r_slice['deviceid'],
                tenantid=tenantid
            )
            if tunnel is None:
                logger.warning('Error: Cannot store tunnel')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add reverse action to the rollback stack
            rollback.push(
                func=storage_helper.remove_tunnel_from_overlay,
                overlayid=overlayid,
                ldeviceid=l_slice['deviceid'],
                rdeviceid=r_slice['deviceid'],
                tenantid=tenantid
            )
            # If the SID list has only one SID, we can store the SID in the
            # IPv6 destination address field of the packets; in this case, the
            # SRH is not required and the encapsulation becomes an IPv4 over
            # IPv6 encapsulation or IPv6 over IPv6 encapsulation
            if (
                not storage_helper.is_srh_forced(
                    r_slice['deviceid'], tenantid
                ) and len(sid_list) == 1
            ):
                # Create a name for the Linux interface and establish the
                # tunnel type
                ip6tnl_ifname = tunnel['tunnel_name']
                ip6tnl_tx_ifname = '%s-%s-tx' % (ip6tnl_ifname, tableid)
                ip6tnl_rx_ifname = '%s-%s-rx' % (ip6tnl_ifname, tableid)
                if overlay_type == OverlayType.IPv4Overlay:
                    tunnel_type = 'ip4ip6'
                elif overlay_type == OverlayType.IPv6Overlay:
                    tunnel_type = 'ip6ip6'
                else:
                    logger.warning(
                        'Error: Unsupported VPN type: %s' % overlay_type
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Create an ip6tnl Linux interface to encapsulate the traffic
                # sent over the tunnel
                # Get a WAN interface
                wan_interfaces = storage_helper.get_wan_interfaces(
                    l_slice['deviceid'], tenantid
                )
                if wan_interfaces is None:
                    # Cannot get WAN interface
                    logger.warning('Cannot get WAN interfaces')
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                if len(wan_interfaces) == 0:
                    # Cannot get WAN interface
                    logger.warning('Cannot get WAN interface')
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                wan_interface = wan_interfaces[0]
                # Get an IPv6 address
                addrs = storage_helper.get_ipv6_addresses(
                    deviceid=l_slice['deviceid'],
                    tenantid=tenantid,
                    interface_name=wan_interface
                )
                if len(addrs) == 0:
                    # No IPv6 address assigned to the interface
                    logging.error(
                        'No IPv6 addresses assigned to the interface %s',
                        wan_interface
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                l_device_wan_ipaddr = addrs[0]
                # Create the ip6tnl interface
                response = self.srv6_manager.create_ip_tunnel_interface(
                    server_ip=l_deviceip,
                    server_port=self.grpc_client_port,
                    ifname=ip6tnl_tx_ifname,
                    local_addr=l_device_wan_ipaddr.split('/')[0],
                    remote_addr=sid_list[0],
                    tunnel_type=tunnel_type
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    logger.warning(
                        'Cannot create the IP Tunnel interface: %s', response
                    )
                    # The operation has failed, return an error message
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.remove_ip_tunnel_interface,
                    server_ip=l_deviceip,
                    server_port=self.grpc_client_port,
                    ifname=ip6tnl_tx_ifname,
                    deviceid=l_slice['deviceid'],
                    tenantid=tenantid
                )
                # Add the tunnel interface to the VRF
                response = self.srv6_manager.update_vrf_device(
                    l_deviceip,
                    self.grpc_client_port,
                    name=overlay_name,
                    interfaces=[ip6tnl_tx_ifname],
                    op='add_interfaces'
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    # If the operation has failed, report an error message
                    logger.warning(
                        'Cannot assign the interface to the VRF: %s', response
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.update_vrf_device,
                    server_ip=l_deviceip,
                    server_port=self.grpc_client_port,
                    name=overlay_name,
                    interfaces=[ip6tnl_tx_ifname],
                    op='del_interfaces',
                    deviceid=l_slice['deviceid'],
                    tenantid=tenantid
                )
                # Set the IP address
                response = self.srv6_manager.create_ipaddr(
                    l_deviceip,
                    self.grpc_client_port,
                    ip_addr=l_device_wan_ipaddr,
                    device=ip6tnl_tx_ifname,
                    family=AF_INET6
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    # If the operation has failed,
                    # report an error message
                    logging.warning(
                        'Cannot assign the IP address to the tunnel interface'
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.remove_ipaddr,
                    server_ip=l_deviceip,
                    server_port=self.grpc_client_port,
                    ip_addr=l_device_wan_ipaddr,
                    device=ip6tnl_tx_ifname,
                    family=AF_INET6,
                    deviceid=l_slice['deviceid'],
                    tenantid=tenantid
                )
                # Create an ip6tnl Linux interface to decapsulate the traffic
                # received over the tunnel
                # Create the ip6tnl interface
                response = self.srv6_manager.create_ip_tunnel_interface(
                    server_ip=r_deviceip,
                    server_port=self.grpc_client_port,
                    ifname=ip6tnl_rx_ifname,
                    local_addr=sid_list[0],
                    remote_addr=l_device_wan_ipaddr.split('/')[0],
                    tunnel_type=tunnel_type
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    logger.warning(
                        'Cannot create the IP Tunnel interface: %s', response
                    )
                    # The operation has failed, return an error message
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.remove_ip_tunnel_interface,
                    server_ip=r_deviceip,
                    server_port=self.grpc_client_port,
                    ifname=ip6tnl_rx_ifname,
                    deviceid=r_slice['deviceid'],
                    tenantid=tenantid
                )
                # Add the tunnel interface to the VRF
                response = self.srv6_manager.update_vrf_device(
                    r_deviceip,
                    self.grpc_client_port,
                    name=overlay_name,
                    interfaces=[ip6tnl_rx_ifname],
                    op='add_interfaces'
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    # If the operation has failed, report an error message
                    logger.warning(
                        'Cannot assign the interface to the VRF: %s',
                        response
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.update_vrf_device,
                    server_ip=r_deviceip,
                    server_port=self.grpc_client_port,
                    name=overlay_name,
                    interfaces=[ip6tnl_rx_ifname],
                    op='del_interfaces',
                    deviceid=r_slice['deviceid'],
                    tenantid=tenantid
                )
                # Get SID prefix length
                public_prefix_length = (
                    storage_helper.get_public_prefix_length(
                        r_slice['deviceid'], tenantid
                    )
                )
                # Set the IP address
                response = self.srv6_manager.create_ipaddr(
                    r_deviceip,
                    self.grpc_client_port,
                    ip_addr=sid_list[0] + '/' + str(public_prefix_length),
                    device=ip6tnl_rx_ifname,
                    family=AF_INET6
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    # If the operation has failed,
                    # report an error message
                    logging.warning(
                        'Cannot assign the IP address to the tunnel interface'
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.remove_ipaddr,
                    server_ip=r_deviceip,
                    server_port=self.grpc_client_port,
                    ip_addr=sid_list[0] + '/' + str(public_prefix_length),
                    device=ip6tnl_rx_ifname,
                    family=AF_INET6,
                    deviceid=r_slice['deviceid'],
                    tenantid=tenantid
                )
            if (
                len(sid_list) > 1
                and (
                    storage_helper.get_outgoing_sr_transparency(
                        l_slice['deviceid'],
                        tenantid
                    ) == 't1'
                    or storage_helper.get_incoming_sr_transparency(
                        r_slice['deviceid'], tenantid
                    ) == 't1'
                )
            ):
                # SID list > 1 and Transparency T1, double encap is required
                # to create the tunnel (IP over IPv6+SRH over IPv6)
                if first_tunnel:
                    # Create a name for the Linux interface and establish the
                    # tunnel type
                    ip6tnl_ifname = tunnel['tunnel_name']
                    ip6tnl_tx_ifname = '%s-%s-tx' % (ip6tnl_ifname, tableid)
                    ip6tnl_rx_ifname = '%s-%s-rx' % (ip6tnl_ifname, tableid)
                    # Create an ip6tnl Linux interface to encapsulate the
                    # traffic sent over the tunnel
                    # Get a WAN interface
                    wan_interfaces = (
                        storage_helper.get_wan_interfaces(
                            l_slice['deviceid'], tenantid
                        )
                    )
                    if wan_interfaces is None:
                        # Cannot get WAN interface
                        logger.warning('Cannot get WAN interfaces')
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    if len(wan_interfaces) == 0:
                        # Cannot get WAN interface
                        logger.warning('Cannot get WAN interface')
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    wan_interface = wan_interfaces[0]
                    # Get an IPv6 address
                    addrs = storage_helper.get_ipv6_addresses(
                        deviceid=l_slice['deviceid'], tenantid=tenantid,
                        interface_name=wan_interface)
                    if len(addrs) == 0:
                        # No IPv6 address assigned to the interface
                        logging.error(
                            'No IPv6 addresses assigned to the interface %s',
                            wan_interface
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    l_device_wan_ipaddr = addrs[0]
                    # Create the ip6tnl interface
                    response = self.srv6_manager.create_ip_tunnel_interface(
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        ifname=ip6tnl_tx_ifname,
                        local_addr=l_device_wan_ipaddr.split('/')[0],
                        remote_addr=sid_list[-1],
                        tunnel_type='ip6ip6'
                    )
                    if response != SbStatusCode.STATUS_SUCCESS:
                        logger.warning(
                            'Cannot create the IP Tunnel interface: %s',
                            response
                        )
                        # The operation has failed, return an error message
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=(
                            self.srv6_manager.remove_ip_tunnel_interface
                        ),
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        ifname=ip6tnl_tx_ifname,
                        deviceid=l_slice['deviceid'],
                        tenantid=tenantid
                    )
                    # Add the tunnel interface to the VRF
                    response = self.srv6_manager.update_vrf_device(
                        l_deviceip,
                        self.grpc_client_port,
                        name=overlay_name,
                        interfaces=[ip6tnl_tx_ifname],
                        op='add_interfaces'
                    )
                    if response != SbStatusCode.STATUS_SUCCESS:
                        # If the operation has failed, report an error message
                        logger.warning(
                            'Cannot assign the interface to the VRF: %s',
                            response
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=(
                            self.srv6_manager.update_vrf_device
                        ),
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        name=overlay_name,
                        interfaces=[ip6tnl_tx_ifname],
                        op='del_interfaces',
                        deviceid=l_slice['deviceid'],
                        tenantid=tenantid
                    )
                    # Set the IP address
                    response = self.srv6_manager.create_ipaddr(
                        l_deviceip,
                        self.grpc_client_port,
                        ip_addr='2001:db9::1/64',
                        device=ip6tnl_tx_ifname,
                        family=AF_INET6
                    )
                    if response != SbStatusCode.STATUS_SUCCESS:
                        # If the operation has failed,
                        # report an error message
                        logging.warning(
                            'Cannot assign the IP address to the tunnel '
                            'interface'
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=(
                            self.srv6_manager.remove_ipaddr
                        ),
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        ip_addr='2001:db9::1/64',
                        device=ip6tnl_tx_ifname,
                        family=AF_INET6,
                        deviceid=l_slice['deviceid'],
                        tenantid=tenantid
                    )
                    # Create an ip6tnl Linux interface to decapsulate the
                    # traffic received over the tunnel
                    # Create the ip6tnl interface
                    response = self.srv6_manager.create_ip_tunnel_interface(
                        server_ip=r_deviceip,
                        server_port=self.grpc_client_port,
                        ifname=ip6tnl_rx_ifname,
                        local_addr=sid_list[-1],
                        remote_addr=l_device_wan_ipaddr.split('/')[0],
                        tunnel_type='ip6ip6'
                    )
                    if response != SbStatusCode.STATUS_SUCCESS:
                        logger.warning(
                            'Cannot create the IP Tunnel interface: %s',
                            response
                        )
                        # The operation has failed, return an error message
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=(
                            self.srv6_manager.remove_ip_tunnel_interface
                        ),
                        server_ip=r_deviceip,
                        server_port=self.grpc_client_port,
                        ifname=ip6tnl_rx_ifname,
                        deviceid=r_slice['deviceid'],
                        tenantid=tenantid
                    )
                    # Add the tunnel interface to the VRF
                    # response = self.srv6_manager.update_vrf_device(
                    #     r_deviceip, self.grpc_client_port, name=overlay_name,
                    #     interfaces=[ip6tnl_rx_ifname],
                    #     op='add_interfaces'
                    # )
                    # if response != SbStatusCode.STATUS_SUCCESS:
                    #     # If the operation has failed, report an error
                    #     # message
                    #     logger.warning(
                    #         'Cannot assign the interface to the VRF: %s',
                    #         response
                    #     )
                    #     return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Get SID prefix length
                    public_prefix_length = (
                        storage_helper.get_public_prefix_length(
                            r_slice['deviceid'], tenantid
                        )
                    )
                    # Set the IP address
                    response = self.srv6_manager.create_ipaddr(
                        r_deviceip, self.grpc_client_port,
                        ip_addr='2001:db9::2/64',
                        device=ip6tnl_rx_ifname, family=AF_INET6
                    )
                    if response != SbStatusCode.STATUS_SUCCESS:
                        # If the operation has failed,
                        # report an error message
                        logging.warning(
                            'Cannot assign the IP address to the tunnel '
                            'interface'
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=(
                            self.srv6_manager.remove_ipaddr
                        ),
                        server_ip=r_deviceip,
                        server_port=self.grpc_client_port,
                        ip_addr='2001:db9::2/64',
                        device=ip6tnl_rx_ifname,
                        family=AF_INET6,
                        deviceid=r_slice['deviceid'],
                        tenantid=tenantid
                    )
            # Create the SRv6 route
            for subnet in subnets:
                subnet = subnet['subnet']
                if not storage_helper.is_srh_forced(
                        r_slice['deviceid'], tenantid) and len(sid_list) == 1:
                    # If we are using an IP over IPv6 encapsulation, we need to
                    # redirect the traffic of the slice over the ip6tnl tunnel
                    response = self.srv6_manager.create_iproute(
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        destination=subnet,
                        out_interface=ip6tnl_tx_ifname,
                        table=tableid
                    )
                    if response != SbStatusCode.STATUS_SUCCESS:
                        # If the operation has failed, report an error message
                        logger.warning(
                            'Cannot set route for %s in %s ',
                            subnet,
                            l_deviceip
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=self.srv6_manager.remove_iproute,
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        destination=subnet,
                        out_interface=ip6tnl_tx_ifname,
                        table=tableid,
                        deviceid=l_slice['deviceid'],
                        tenantid=tenantid
                    )
                else:
                    if (
                        storage_helper.get_outgoing_sr_transparency(
                            l_slice['deviceid'], tenantid
                        ) == 't1'
                        or storage_helper.get_incoming_sr_transparency(
                            r_slice['deviceid'], tenantid
                        ) == 't1'
                    ):
                        response = self.srv6_manager.create_srv6_explicit_path(
                            l_deviceip,
                            self.grpc_client_port,
                            destination=subnet,
                            table=tableid,
                            device=dev,
                            segments=sid_list[:-1],
                            encapmode='encap'
                        )
                        if response != SbStatusCode.STATUS_SUCCESS:
                            # If the operation has failed, report an error
                            # message
                            logger.warning(
                                'Cannot create SRv6 Explicit Path: %s',
                                response
                            )
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=self.exec_or_mark_device_inconsitent,
                            rollback_func=(
                                self.srv6_manager.remove_srv6_explicit_path
                            ),
                            server_ip=l_deviceip,
                            server_port=self.grpc_client_port,
                            destination=subnet,
                            device=dev,
                            segments=sid_list[:-1],
                            encapmode="encap",
                            table=tableid,
                            deviceid=l_slice['deviceid'],
                            tenantid=tenantid
                        )
                        response = self.srv6_manager.create_iproute(
                            server_ip=l_deviceip,
                            server_port=self.grpc_client_port,
                            destination=sid_list[-2],
                            out_interface=ip6tnl_tx_ifname,
                            table=tableid
                        )
                        if response != SbStatusCode.STATUS_SUCCESS:
                            # If the operation has failed, report an error
                            # message
                            logger.warning(
                                'Cannot set route for %s in %s ',
                                subnet,
                                l_deviceip
                            )
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=self.exec_or_mark_device_inconsitent,
                            rollback_func=self.srv6_manager.remove_iproute,
                            server_ip=l_deviceip,
                            server_port=self.grpc_client_port,
                            destination=sid_list[-2],
                            out_interface=ip6tnl_tx_ifname,
                            table=tableid,
                            deviceid=l_slice['deviceid'],
                            tenantid=tenantid
                        )
                    else:
                        # If we are using an IP over IPv6+SRH encapsulation,
                        # we need to redirect the traffic over the SRH tunnel
                        response = self.srv6_manager.create_srv6_explicit_path(
                            l_deviceip,
                            self.grpc_client_port,
                            destination=subnet,
                            table=tableid,
                            device=dev,
                            segments=sid_list,
                            encapmode='encap'
                        )
                        if response != SbStatusCode.STATUS_SUCCESS:
                            # If the operation has failed, report an error
                            # message
                            logger.warning(
                                'Cannot create SRv6 Explicit Path: %s',
                                response
                            )
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=self.exec_or_mark_device_inconsitent,
                            rollback_func=(
                                self.srv6_manager.remove_srv6_explicit_path
                            ),
                            server_ip=l_deviceip,
                            server_port=self.grpc_client_port,
                            destination=subnet,
                            device=dev,
                            segments=sid_list,
                            encapmode="encap",
                            table=tableid,
                            deviceid=l_slice['deviceid'],
                            tenantid=tenantid
                        )
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug('Remote interface assigned to VPN successfully')
        return NbStatusCode.STATUS_OK

    def _remove_tunnel_uni(
        self,
        overlayid,
        overlay_name,
        overlay_type,
        l_slice,
        r_slice,
        tenantid,
        overlay_info,
        ignore_errors=False
    ):
        with RollbackContext() as rollback:
            # Decrease the number of tunnels
            num_tunnels = (
                storage_helper.dec_and_get_tunnels_counter(
                    overlayid, tenantid, l_slice['deviceid'], r_slice)
            )
            # Add reverse action to the rollback stack
            rollback.push(
                func=storage_helper.inc_and_get_tunnels_counter,
                overlayid=overlayid,
                tenantid=tenantid,
                deviceid=l_slice['deviceid'],
                dest_slice=r_slice
            )
            # Check if there are other unidirectional tunnels
            # between the two slices
            # If the uni tunnel already exists, we have done
            if num_tunnels is not None and num_tunnels > 0:
                # Success, commit all performed operations
                rollback.commitAll()
                return NbStatusCode.STATUS_OK
            # Remove the tunnel
            #
            # Get router address
            l_deviceip = storage_helper.get_router_mgmtip(
                l_slice['deviceid'], tenantid
            )
            if l_deviceip is None:
                # Cannot get the router address
                logger.warning('Cannot get the router address')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get router address
            r_deviceip = storage_helper.get_router_mgmtip(
                r_slice['deviceid'], tenantid
            )
            if r_deviceip is None:
                # Cannot get the router address
                logger.warning('Cannot get the router address')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get the table ID
            tableid = storage_helper.get_tableid(
                overlayid, tenantid)
            if tableid is None:
                logger.warning('Cannot retrieve VPN table ID')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get the subnets
            if overlay_type == OverlayType.IPv6Overlay:
                subnets = storage_helper.get_ipv6_subnets(
                    r_slice['deviceid'], tenantid, r_slice['interface_name']
                )
            elif overlay_type == OverlayType.IPv4Overlay:
                subnets = storage_helper.get_ipv4_subnets(
                    r_slice['deviceid'], tenantid, r_slice['interface_name']
                )
            else:
                logger.warning(
                    'Error: Unsupported VPN type: %s', overlay_type
                )
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get the SID
            sid_list = self.controller_state_srv6.get_sid_list(
                r_slice['deviceid'], tenantid, tableid
            )
            # pyroute2 requires SID list in reverse order
            sid_list = sid_list[::-1]
            # Remove the SRv6 route
            for subnet in subnets:
                subnet = subnet['subnet']
                if (
                    not storage_helper.is_srh_forced(
                        r_slice['deviceid'], tenantid
                    ) and len(sid_list) == 1
                ):
                    # Get tunnel
                    tunnel = storage_helper.get_tunnel(
                        overlayid=overlayid,
                        ldeviceid=l_slice['deviceid'],
                        rdeviceid=r_slice['deviceid'],
                        tenantid=tenantid
                    )
                    if tunnel is None:
                        logger.warning('Error: Cannot store tunnel')
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    ip6tnl_ifname = tunnel['tunnel_name']
                    ip6tnl_tx_ifname = '%s-%s-tx' % (ip6tnl_ifname, tableid)
                    ip6tnl_rx_ifname = '%s-%s-rx' % (ip6tnl_ifname, tableid)
                    # If we are using an IP over IPv6 encapsulation, we need to
                    # remove the route
                    response = self.srv6_manager.remove_iproute(
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        destination=subnet,
                        out_interface=ip6tnl_tx_ifname,
                        table=tableid
                    )
                    if response != SbStatusCode.STATUS_SUCCESS:
                        if ignore_errors:
                            logger.warning(
                                'Cannot set route for %s in %s ',
                                subnet,
                                l_deviceip
                            )
                            # Change device state to reboot required
                            success = (
                                storage_helper.change_device_state(
                                    deviceid=l_slice['deviceid'],
                                    tenantid=tenantid,
                                    new_state=DeviceState.REBOOT_REQUIRED
                                )
                            )
                            if success is False or success is None:
                                logging.error(
                                    'Error changing the device state'
                                )
                                return status_codes_pb2.STATUS_INTERNAL_ERROR
                        else:
                            # If the operation has failed, report an error
                            # message
                            logger.warning(
                                'Cannot set route for %s in %s ',
                                subnet,
                                l_deviceip
                            )
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=self.srv6_manager.create_iproute,
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        destination=subnet,
                        out_interface=ip6tnl_tx_ifname,
                        table=tableid,
                        deviceid=l_slice['deviceid'],
                        tenantid=tenantid
                    )
                else:
                    if (
                        storage_helper.get_outgoing_sr_transparency(
                            l_slice['deviceid'], tenantid
                        ) == 't1'
                        or storage_helper.get_incoming_sr_transparency(
                            r_slice['deviceid'], tenantid) == 't1'
                    ):
                        # Get tunnel
                        tunnel = storage_helper.get_tunnel(
                            overlayid=overlayid,
                            ldeviceid=l_slice['deviceid'],
                            rdeviceid=r_slice['deviceid'],
                            tenantid=tenantid
                        )
                        if tunnel is None:
                            logger.warning('Error: Cannot store tunnel')
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                        ip6tnl_ifname = tunnel['tunnel_name']
                        ip6tnl_tx_ifname = (
                            '%s-%s-tx' % (ip6tnl_ifname, tableid)
                        )
                        ip6tnl_rx_ifname = (
                            '%s-%s-rx' % (ip6tnl_ifname, tableid)
                        )
                        response = self.srv6_manager.remove_iproute(
                            server_ip=l_deviceip,
                            server_port=self.grpc_client_port,
                            destination=sid_list[-2],
                            out_interface=ip6tnl_tx_ifname,
                            table=tableid
                        )
                        if response != SbStatusCode.STATUS_SUCCESS:
                            if ignore_errors:
                                logger.warning(
                                    'Cannot remove route for %s in %s ',
                                    subnet,
                                    l_deviceip
                                )
                                # Change device state to reboot required
                                success = storage_helper.change_device_state(
                                    deviceid=l_slice['deviceid'],
                                    tenantid=tenantid,
                                    new_state=DeviceState.REBOOT_REQUIRED
                                )
                                if success is False or success is None:
                                    logging.error(
                                        'Error changing the device state'
                                    )
                                    return (
                                        status_codes_pb2.STATUS_INTERNAL_ERROR
                                    )
                            else:
                                # If the operation has failed, report an error
                                # message
                                logger.warning(
                                    'Cannot remove route for %s in %s ',
                                    subnet,
                                    l_deviceip
                                )
                                return (
                                    NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                                )
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=self.exec_or_mark_device_inconsitent,
                            rollback_func=self.srv6_manager.create_iproute,
                            server_ip=l_deviceip,
                            server_port=self.grpc_client_port,
                            destination=sid_list[-2],
                            out_interface=ip6tnl_tx_ifname,
                            table=tableid,
                            deviceid=l_slice['deviceid'],
                            tenantid=tenantid
                        )
                        _dev = storage_helper.get_wan_interfaces(
                            l_slice['deviceid'], tenantid
                        )
                        if _dev is None:
                            # Cannot get non-loopback interface
                            logger.warning('Cannot get non-loopback interface')
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                        if len(_dev) == 0:
                            # Cannot get wan interface
                            logger.warning(
                                'Cannot get non-loopback interface. '
                                'No WAN interfaces'
                            )
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                        _dev = _dev[0]
                        response = self.srv6_manager.remove_srv6_explicit_path(
                            l_deviceip,
                            self.grpc_client_port,
                            destination=subnet,
                            table=tableid,
                            device=_dev,
                            segments=sid_list[:-1],
                            encapmode='encap'
                        )
                        if response != SbStatusCode.STATUS_SUCCESS:
                            if ignore_errors:
                                logger.warning(
                                    'Cannot remove SRv6 Explicit Path: %s',
                                    response
                                )
                                # Change device state to reboot required
                                success = storage_helper.change_device_state(
                                    deviceid=l_slice['deviceid'],
                                    tenantid=tenantid,
                                    new_state=DeviceState.REBOOT_REQUIRED
                                )
                                if success is False or success is None:
                                    logging.error(
                                        'Error changing the device state'
                                    )
                                    return (
                                        status_codes_pb2.STATUS_INTERNAL_ERROR
                                    )
                            else:
                                # If the operation has failed, report an error
                                # message
                                logger.warning(
                                    'Cannot remove SRv6 Explicit Path: %s',
                                    response
                                )
                                return (
                                    NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                                )
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=self.exec_or_mark_device_inconsitent,
                            rollback_func=(
                                self.srv6_manager.create_srv6_explicit_path
                            ),
                            server_ip=l_deviceip,
                            server_port=self.grpc_client_port,
                            destination=subnet,
                            device=_dev,
                            segments=sid_list[:-1],
                            encapmode="encap",
                            table=tableid,
                            deviceid=l_slice['deviceid'],
                            tenantid=tenantid
                        )
                    else:
                        # If we are using an IP over IPv6+SRH encapsulation,
                        # we need to remove the SRv6 route
                        response = self.srv6_manager.remove_srv6_explicit_path(
                            l_deviceip,
                            self.grpc_client_port,
                            destination=subnet,
                            table=tableid
                        )
                        if response != SbStatusCode.STATUS_SUCCESS:
                            if ignore_errors:
                                # Change device state to reboot required
                                success = storage_helper.change_device_state(
                                    deviceid=l_slice['deviceid'],
                                    tenantid=tenantid,
                                    new_state=DeviceState.REBOOT_REQUIRED
                                )
                                if success is False or success is None:
                                    logging.error(
                                        'Error changing the device state'
                                    )
                                    return (
                                        status_codes_pb2.STATUS_INTERNAL_ERROR
                                    )
                            else:
                                # If the operation has failed, return an error
                                # message
                                logger.warning(
                                    'Cannot remove SRv6 Explicit Path: %s',
                                    response
                                )
                                return (
                                    NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                                )
                        _dev = storage_helper.get_wan_interfaces(
                            l_slice['deviceid'], tenantid
                        )
                        if _dev is None:
                            # Cannot get non-loopback interface
                            logger.warning('Cannot get non-loopback interface')
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                        if len(_dev) == 0:
                            # Cannot get wan interface
                            logger.warning(
                                'Cannot get non-loopback interface. '
                                'No WAN interfaces'
                            )
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                        _dev = _dev[0]
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=self.exec_or_mark_device_inconsitent,
                            rollback_func=(
                                self.srv6_manager.create_srv6_explicit_path
                            ),
                            server_ip=l_deviceip,
                            server_port=self.grpc_client_port,
                            destination=subnet,
                            device=_dev,
                            segments=sid_list,
                            encapmode="encap",
                            table=tableid,
                            deviceid=l_slice['deviceid'],
                            tenantid=tenantid
                        )
            # If the SID list has only one SID and we are using the ip6tnl
            # interface to encapsulate the traffic over an IPv6 tunnel
            if (
                not storage_helper.is_srh_forced(
                    r_slice['deviceid'], tenantid
                ) and len(sid_list) == 1
            ):
                # Get tunnel
                tunnel = storage_helper.get_tunnel(
                    overlayid=overlayid,
                    ldeviceid=l_slice['deviceid'],
                    rdeviceid=r_slice['deviceid'],
                    tenantid=tenantid
                )
                if tunnel is None:
                    logger.warning('Error: Cannot store tunnel')
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                ip6tnl_ifname = tunnel['tunnel_name']
                ip6tnl_tx_ifname = '%s-%s-tx' % (ip6tnl_ifname, tableid)
                ip6tnl_rx_ifname = '%s-%s-rx' % (ip6tnl_ifname, tableid)
                # Get a WAN interface
                wan_interfaces = storage_helper.get_wan_interfaces(
                    l_slice['deviceid'], tenantid
                )
                if wan_interfaces is None:
                    # Cannot get WAN interface
                    logger.warning('Cannot get WAN interfaces')
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                if len(wan_interfaces) == 0:
                    # Cannot get WAN interface
                    logger.warning('Cannot get WAN interface')
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                wan_interface = wan_interfaces[0]
                # Get an IPv6 address
                addrs = storage_helper.get_ipv6_addresses(
                    deviceid=l_slice['deviceid'],
                    tenantid=tenantid,
                    interface_name=wan_interface
                )
                if len(addrs) == 0:
                    # No IPv6 address assigned to the interface
                    logging.error(
                        'No IPv6 addresses assigned to the interface %s',
                        wan_interface
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                l_device_wan_ipaddr = addrs[0]
                # Get SID prefix length
                public_prefix_length = (
                    storage_helper.get_public_prefix_length(
                        r_slice['deviceid'], tenantid
                    )
                )
                # Remove the IP address
                response = self.srv6_manager.remove_ipaddr(
                    r_deviceip,
                    self.grpc_client_port,
                    ip_addr=sid_list[0] + '/' + str(public_prefix_length),
                    device=ip6tnl_rx_ifname,
                    family=AF_INET6
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    if ignore_errors:
                        # Change device state to reboot required
                        success = storage_helper.change_device_state(
                            deviceid=r_slice['deviceid'],
                            tenantid=tenantid,
                            new_state=DeviceState.REBOOT_REQUIRED
                        )
                        if success is False or success is None:
                            logging.error('Error changing the device state')
                            return status_codes_pb2.STATUS_INTERNAL_ERROR
                    else:
                        # If the operation has failed, report an error message
                        logging.warning(
                            'Cannot remove the IP address from the tunnel '
                            'interface'
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.create_ipaddr,
                    server_ip=r_deviceip,
                    server_port=self.grpc_client_port,
                    ip_addr=sid_list[0] + '/' + str(public_prefix_length),
                    device=ip6tnl_rx_ifname,
                    family=AF_INET6,
                    deviceid=r_slice['deviceid'],
                    tenantid=tenantid
                )
                # Remove the ip6 tunnel decap interface from the VRF
                response = self.srv6_manager.update_vrf_device(
                    r_deviceip,
                    self.grpc_client_port,
                    name=overlay_name,
                    interfaces=[ip6tnl_rx_ifname],
                    op='del_interfaces'
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    if ignore_errors:
                        # Change device state to reboot required
                        success = storage_helper.change_device_state(
                            deviceid=r_slice['deviceid'],
                            tenantid=tenantid,
                            new_state=DeviceState.REBOOT_REQUIRED
                        )
                        if success is False or success is None:
                            logging.error('Error changing the device state')
                            return status_codes_pb2.STATUS_INTERNAL_ERROR
                    else:
                        # If the operation has failed, report an error message
                        logger.warning(
                            'Cannot remove the ip6tnl interface from the '
                            'VRF: %s',
                            response
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.update_vrf_device,
                    server_ip=r_deviceip,
                    server_port=self.grpc_client_port,
                    name=overlay_name,
                    interfaces=[ip6tnl_rx_ifname],
                    op='add_interfaces',
                    deviceid=r_slice['deviceid'],
                    tenantid=tenantid
                )
                # Remove the ip6tnl Linux interface used for decapsulation
                response = self.srv6_manager.remove_ip_tunnel_interface(
                    server_ip=r_deviceip,
                    server_port=self.grpc_client_port,
                    ifname=ip6tnl_rx_ifname
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    if ignore_errors:
                        # Change device state to reboot required
                        success = storage_helper.change_device_state(
                            deviceid=r_slice['deviceid'],
                            tenantid=tenantid,
                            new_state=DeviceState.REBOOT_REQUIRED
                        )
                        if success is False or success is None:
                            logging.error('Error changing the device state')
                            return status_codes_pb2.STATUS_INTERNAL_ERROR
                    else:
                        logger.warning(
                            'Cannot remove the IP Tunnel interface: %s',
                            response
                        )
                        # The operation has failed, return an error message
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                if overlay_type == OverlayType.IPv4Overlay:
                    tunnel_type = 'ip4ip6'
                elif overlay_type == OverlayType.IPv6Overlay:
                    tunnel_type = 'ip6ip6'
                else:
                    logger.warning(
                        'Error: Unsupported VPN type: %s', overlay_type
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.create_ip_tunnel_interface,
                    server_ip=r_deviceip,
                    server_port=self.grpc_client_port,
                    ifname=ip6tnl_rx_ifname,
                    local_addr=sid_list[0],
                    remote_addr=l_device_wan_ipaddr.split('/')[0],
                    tunnel_type=tunnel_type,
                    deviceid=r_slice['deviceid'],
                    tenantid=tenantid
                )
                # Remove the IP address of the encapsulation ip6tnl interface
                response = self.srv6_manager.remove_ipaddr(
                    l_deviceip,
                    self.grpc_client_port,
                    ip_addr=l_device_wan_ipaddr,
                    device=ip6tnl_tx_ifname,
                    family=AF_INET6
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    if ignore_errors:
                        # Change device state to reboot required
                        success = storage_helper.change_device_state(
                            deviceid=l_slice['deviceid'],
                            tenantid=tenantid,
                            new_state=DeviceState.REBOOT_REQUIRED
                        )
                        if success is False or success is None:
                            logging.error('Error changing the device state')
                            return status_codes_pb2.STATUS_INTERNAL_ERROR
                    else:
                        # If the operation has failed,
                        # report an error message
                        logging.warning(
                            'Cannot remove the IP address from the tunnel '
                            'interface'
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.create_ipaddr,
                    server_ip=l_deviceip,
                    server_port=self.grpc_client_port,
                    ip_addr=l_device_wan_ipaddr,
                    device=ip6tnl_tx_ifname,
                    family=AF_INET6,
                    deviceid=l_slice['deviceid'],
                    tenantid=tenantid
                )
                # Remove the ip6tnl encap interface from the VRF
                response = self.srv6_manager.update_vrf_device(
                    l_deviceip,
                    self.grpc_client_port,
                    name=overlay_name,
                    interfaces=[ip6tnl_tx_ifname],
                    op='del_interfaces'
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    if ignore_errors:
                        # Change device state to reboot required
                        success = storage_helper.change_device_state(
                            deviceid=l_slice['deviceid'],
                            tenantid=tenantid,
                            new_state=DeviceState.REBOOT_REQUIRED
                        )
                        if success is False or success is None:
                            logging.error('Error changing the device state')
                            return status_codes_pb2.STATUS_INTERNAL_ERROR
                    else:
                        # If the operation has failed, report an error message
                        logger.warning(
                            'Cannot remove the ip6tnl interface from the '
                            'VRF: %s',
                            response
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.update_vrf_device,
                    server_ip=l_deviceip,
                    server_port=self.grpc_client_port,
                    name=overlay_name,
                    interfaces=[ip6tnl_tx_ifname],
                    op='add_interfaces',
                    deviceid=l_slice['deviceid'],
                    tenantid=tenantid
                )
                # Remove the ip6tnl encap interface
                response = self.srv6_manager.remove_ip_tunnel_interface(
                    server_ip=l_deviceip, server_port=self.grpc_client_port,
                    ifname=ip6tnl_tx_ifname
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    if ignore_errors:
                        # Change device state to reboot required
                        success = storage_helper.change_device_state(
                            deviceid=l_slice['deviceid'],
                            tenantid=tenantid,
                            new_state=DeviceState.REBOOT_REQUIRED
                        )
                        if success is False or success is None:
                            logging.error('Error changing the device state')
                            return status_codes_pb2.STATUS_INTERNAL_ERROR
                    else:
                        logger.warning(
                            'Cannot remove the IP Tunnel interface: %s',
                            response
                        )
                        # The operation has failed, return an error message
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.create_ip_tunnel_interface,
                    server_ip=l_deviceip,
                    server_port=self.grpc_client_port,
                    ifname=ip6tnl_tx_ifname,
                    local_addr=l_device_wan_ipaddr.split('/')[0],
                    remote_addr=sid_list[0],
                    tunnel_type=tunnel_type,
                    deviceid=l_slice['deviceid'],
                    tenantid=tenantid
                )
            # Remove the tunnel from the controller state
            tunnel = storage_helper.remove_tunnel_from_overlay(
                overlayid=overlayid, ldeviceid=l_slice['deviceid'],
                rdeviceid=r_slice['deviceid'], tenantid=tenantid
            )
            if tunnel is None:
                logger.warning('Error: Cannot remove tunnel')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add reverse action to the rollback stack
            rollback.push(
                func=storage_helper.add_tunnel_to_overlay,
                overlayid=overlayid,
                ldeviceid=l_slice['deviceid'],
                rdeviceid=r_slice['deviceid'],
                tenantid=tenantid
            )
            if (
                storage_helper.get_outgoing_sr_transparency(
                    l_slice['deviceid'], tenantid
                ) == 't1'
                or storage_helper.get_incoming_sr_transparency(
                    r_slice['deviceid'], tenantid
                ) == 't1'
            ):
                # Get tunnel
                _tunnel = storage_helper.get_tunnel(
                    overlayid=overlayid,
                    ldeviceid=l_slice['deviceid'],
                    rdeviceid=r_slice['deviceid'],
                    tenantid=tenantid
                )
                last_tunnel = False
                if _tunnel is None:
                    last_tunnel = True
                ip6tnl_ifname = tunnel['tunnel_name']
                ip6tnl_tx_ifname = '%s-%s-tx' % (ip6tnl_ifname, tableid)
                ip6tnl_rx_ifname = '%s-%s-rx' % (ip6tnl_ifname, tableid)
                # SID list > 1 and Transparency T1, double encap is required
                # to create the tunnel (IP over IPv6+SRH over IPv6)
                if last_tunnel:
                    # Create an ip6tnl Linux interface to encapsulate the
                    # traffic sent over the tunnel
                    # Get a WAN interface
                    wan_interfaces = storage_helper.get_wan_interfaces(
                        l_slice['deviceid'], tenantid
                    )
                    if wan_interfaces is None:
                        # Cannot get WAN interface
                        logger.warning('Cannot get WAN interfaces')
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    if len(wan_interfaces) == 0:
                        # Cannot get WAN interface
                        logger.warning('Cannot get WAN interface')
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    wan_interface = wan_interfaces[0]
                    # Get an IPv6 address
                    addrs = storage_helper.get_ipv6_addresses(
                        deviceid=l_slice['deviceid'],
                        tenantid=tenantid,
                        interface_name=wan_interface
                    )
                    if len(addrs) == 0:
                        # No IPv6 address assigned to the interface
                        logging.error(
                            'No IPv6 addresses assigned to the interface %s',
                            wan_interface
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    l_device_wan_ipaddr = addrs[0]
                    # Get SID prefix length
                    public_prefix_length = (
                        storage_helper.get_public_prefix_length(
                            r_slice['deviceid'], tenantid
                        )
                    )
                    # Set the IP address
                    response = self.srv6_manager.remove_ipaddr(
                        r_deviceip,
                        self.grpc_client_port,
                        ip_addr='2001:db9::2/64',
                        device=ip6tnl_rx_ifname,
                        family=AF_INET6
                    )
                    if response != SbStatusCode.STATUS_SUCCESS:
                        if ignore_errors:
                            # Change device state to reboot required
                            success = storage_helper.change_device_state(
                                deviceid=r_slice['deviceid'],
                                tenantid=tenantid,
                                new_state=DeviceState.REBOOT_REQUIRED
                            )
                            if success is False or success is None:
                                logging.error(
                                    'Error changing the device state'
                                )
                                return status_codes_pb2.STATUS_INTERNAL_ERROR
                        else:
                            # If the operation has failed,
                            # report an error message
                            logging.warning(
                                'Cannot remove the IP address of the tunnel '
                                'interface'
                            )
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=self.srv6_manager.create_ipaddr,
                        server_ip=r_deviceip,
                        server_port=self.grpc_client_port,
                        ip_addr='2001:db9::2/64',
                        device=ip6tnl_rx_ifname,
                        family=AF_INET6,
                        deviceid=r_slice['deviceid'],
                        tenantid=tenantid
                    )
                    # Add the tunnel interface to the VRF
                    # response = self.srv6_manager.update_vrf_device(
                    #     r_deviceip, self.grpc_client_port, name=overlay_name,
                    #     interfaces=[ip6tnl_rx_ifname],
                    #     op='del_interfaces'
                    # )
                    # if response != SbStatusCode.STATUS_SUCCESS:
                    #     # If the operation has failed, report an error
                    #     # message
                    #     logger.warning(
                    #         'Cannot remove the interface from the VRF: %s'
                    #         % response
                    #     )
                    #     return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Create an ip6tnl Linux interface to decapsulate the
                    # traffic received over the tunnel
                    # Create the ip6tnl interface
                    response = self.srv6_manager.remove_ip_tunnel_interface(
                        server_ip=r_deviceip,
                        server_port=self.grpc_client_port,
                        ifname=ip6tnl_rx_ifname
                    )
                    if response != SbStatusCode.STATUS_SUCCESS:
                        if ignore_errors:
                            # Change device state to reboot required
                            success = storage_helper.change_device_state(
                                deviceid=r_slice['deviceid'],
                                tenantid=tenantid,
                                new_state=DeviceState.REBOOT_REQUIRED
                            )
                            if success is False or success is None:
                                logging.error(
                                    'Error changing the device state'
                                )
                                return status_codes_pb2.STATUS_INTERNAL_ERROR
                        else:
                            logger.warning(
                                'Cannot remove the IP Tunnel interface: %s',
                                response
                            )
                            # The operation has failed, return an error message
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=(
                            self.srv6_manager.create_ip_tunnel_interface
                        ),
                        server_ip=r_deviceip,
                        server_port=self.grpc_client_port,
                        ifname=ip6tnl_rx_ifname,
                        local_addr=sid_list[-1],
                        remote_addr=l_device_wan_ipaddr.split('/')[0],
                        tunnel_type='ip6ip6',
                        deviceid=r_slice['deviceid'],
                        tenantid=tenantid
                    )
                    # Set the IP address
                    response = self.srv6_manager.remove_ipaddr(
                        l_deviceip,
                        self.grpc_client_port,
                        ip_addr='2001:db9::1/64',
                        device=ip6tnl_tx_ifname, family=AF_INET6
                    )
                    if response != SbStatusCode.STATUS_SUCCESS:
                        if ignore_errors:
                            # Change device state to reboot required
                            success = storage_helper.change_device_state(
                                deviceid=l_slice['deviceid'],
                                tenantid=tenantid,
                                new_state=DeviceState.REBOOT_REQUIRED
                            )
                            if success is False or success is None:
                                logging.error(
                                    'Error changing the device state'
                                )
                                return status_codes_pb2.STATUS_INTERNAL_ERROR
                        else:
                            # If the operation has failed,
                            # report an error message
                            logging.warning(
                                'Cannot remove the IP address of the tunnel '
                                'interface'
                            )
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=self.srv6_manager.create_ipaddr,
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        ip_addr='2001:db9::1/64',
                        device=ip6tnl_tx_ifname,
                        family=AF_INET6,
                        deviceid=l_slice['deviceid'],
                        tenantid=tenantid
                    )
                    # Add the tunnel interface to the VRF
                    response = self.srv6_manager.update_vrf_device(
                        l_deviceip,
                        self.grpc_client_port,
                        name=overlay_name,
                        interfaces=[ip6tnl_tx_ifname],
                        op='del_interfaces'
                    )
                    if response != SbStatusCode.STATUS_SUCCESS:
                        if ignore_errors:
                            # Change device state to reboot required
                            success = storage_helper.change_device_state(
                                deviceid=l_slice['deviceid'],
                                tenantid=tenantid,
                                new_state=DeviceState.REBOOT_REQUIRED
                            )
                            if success is False or success is None:
                                logging.error(
                                    'Error changing the device state'
                                )
                                return status_codes_pb2.STATUS_INTERNAL_ERROR
                        else:
                            # If the operation has failed, report an error
                            # message
                            logger.warning(
                                'Cannot remove the interface from the VRF: %s',
                                response
                            )
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=self.srv6_manager.update_vrf_device,
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        name=overlay_name,
                        interfaces=[ip6tnl_tx_ifname],
                        op='add_interfaces',
                        deviceid=l_slice['deviceid'],
                        tenantid=tenantid
                    )
                    # Create the ip6tnl interface
                    response = self.srv6_manager.remove_ip_tunnel_interface(
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        ifname=ip6tnl_tx_ifname
                    )
                    if response != SbStatusCode.STATUS_SUCCESS:
                        if ignore_errors:
                            # Change device state to reboot required
                            success = storage_helper.change_device_state(
                                deviceid=l_slice['deviceid'],
                                tenantid=tenantid,
                                new_state=DeviceState.REBOOT_REQUIRED
                            )
                            if success is False or success is None:
                                logging.error(
                                    'Error changing the device state'
                                )
                                return status_codes_pb2.STATUS_INTERNAL_ERROR
                        else:
                            logger.warning(
                                'Cannot remove the IP Tunnel interface: %s',
                                response
                            )
                            # The operation has failed, return an error message
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=(
                            self.srv6_manager.create_ip_tunnel_interface
                        ),
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        ifname=ip6tnl_tx_ifname,
                        local_addr=l_device_wan_ipaddr.split('/')[0],
                        remote_addr=sid_list[-1],
                        tunnel_type='ip6ip6',
                        deviceid=l_slice['deviceid'],
                        tenantid=tenantid
                    )
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug('Remove unidirectional tunnel completed')
        return NbStatusCode.STATUS_OK

    def init_overlay_data(
        self,
        overlayid,
        overlay_name,
        tenantid,
        overlay_info
    ):
        logger.debug(
            'Initiating overlay data for the overlay %s', overlay_name
        )
        with RollbackContext() as rollback:
            # Initialize the overlay data structure
            #
            # Get a new table ID for the overlay
            logger.debug('Attempting to get a new table ID for the VPN')
            tableid = storage_helper.get_new_tableid(
                overlayid, tenantid
            )
            logger.debug('New table ID assigned to the VPN: %s', tableid)
            # Add reverse action to the rollback stack
            rollback.push(
                func=storage_helper.release_tableid,
                overlayid=overlayid,
                tenantid=tenantid
            )
            logger.debug('Validating the table ID: %s' % tableid)
            # Validate the table ID
            if not srv6_controller_utils.validate_table_id(tableid):
                logger.warning('Invalid table ID: %s' % tableid)
                # If the table ID is not valid, return an error message
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug(
            'Init overlay data completed for the overlay %s', overlay_name
        )
        return NbStatusCode.STATUS_OK

    def init_tunnel_mode(self, deviceid, tenantid, overlay_info):
        logger.debug(
            'Initiating tunnel mode on router %s', deviceid
        )
        with RollbackContext() as rollback:
            # Initialize the tunnel mode on the router
            #
            # Get the router address
            deviceip = storage_helper.get_device_mgmtip(
                tenantid, deviceid
            )
            if deviceip is None:
                # Cannot get the router address
                logger.warning('Cannot get the router address')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # First step: create a rule for local SIDs processing
            # This step is just required for the first VPN
            #
            # Get SID family for this router
            sid_family = self.controller_state_srv6.get_sid_family(
                deviceid, tenantid
            )
            if sid_family is None:
                # If the operation has failed, return an error message
                logger.warning(
                    'Cannot get SID family for deviceid %s', deviceid
                )
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add the rule to steer the SIDs through the local SID table
            # Note: by default there is already an ip rule to steer the packets
            # through the main routing table; therefore, if the local SID table
            # is the main routing table, we don't need to add an ip rule and we
            # can skip this step
            if srv6_controller_utils.LOCAL_SID_TABLE != \
                    srv6_controller_utils.MAIN_ROUTING_TABLE:
                response = self.srv6_manager.create_iprule(
                    deviceip,
                    self.grpc_client_port,
                    family=AF_INET6,
                    table=srv6_controller_utils.LOCAL_SID_TABLE,
                    destination=sid_family
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    logger.warning(
                        'Cannot create the IP rule for destination %s: %s',
                        sid_family,
                        response
                    )
                    # If the operation has failed, return an error message
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.remove_iprule,
                    server_ip=deviceip,
                    server_port=self.grpc_client_port,
                    family=AF_INET6,
                    table=srv6_controller_utils.LOCAL_SID_TABLE,
                    destination=sid_family,
                    deviceid=deviceid,
                    tenantid=tenantid
                )
            # Add a blackhole route to drop all unknown active segments
            # If the local SID table used to store the segments is the main
            # table, we skip this step
            if srv6_controller_utils.LOCAL_SID_TABLE != \
                    srv6_controller_utils.MAIN_ROUTING_TABLE:
                response = self.srv6_manager.create_iproute(
                    deviceip,
                    self.grpc_client_port,
                    family=AF_INET6,
                    type='blackhole',
                    table=srv6_controller_utils.LOCAL_SID_TABLE
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    logger.warning(
                        'Cannot create the blackhole route: %s', response
                    )
                    # If the operation has failed, return an error message
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.remove_iproute,
                    server_ip=deviceip,
                    server_port=self.grpc_client_port,
                    family=AF_INET6,
                    type='blackhole',
                    table=srv6_controller_utils.LOCAL_SID_TABLE,
                    deviceid=deviceid,
                    tenantid=tenantid
                )
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug(
            'Init tunnel mode on device %s completed', deviceid
        )
        return NbStatusCode.STATUS_OK

    def init_overlay(
        self,
        overlayid,
        overlay_name,
        overlay_type,
        tenantid,
        deviceid,
        overlay_info
    ):
        logger.debug(
            'Initiating overlay %s on the device %s', overlay_name, deviceid
        )
        with RollbackContext() as rollback:
            # Get the router address
            deviceip = storage_helper.get_device_mgmtip(
                tenantid, deviceid
            )
            if deviceip is None:
                # Cannot get the router address
                logger.warning('Cannot get the router address')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Second step is the creation of the decapsulation and lookup route
            if overlay_type == 'IPv6Overlay':
                # For IPv6 VPN we have to perform decap and lookup in IPv6
                # routing table. This behavior is realized by End.DT6 SRv6
                # action
                action = 'End.DT6'
            elif overlay_type == 'IPv4Overlay':
                # For IPv4 VPN we have to perform decap and lookup in IPv4
                # routing table. This behavior is realized by End.DT4 SRv6
                # action
                action = 'End.DT4'
            else:
                logger.warning(
                    'Error: Unsupported VPN type: %s', overlay_type
                )
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get the table ID for the VPN
            logger.debug(
                'Attempting to retrieve the table ID assigned to the VPN'
            )
            tableid = storage_helper.get_tableid(
                overlayid, tenantid
            )
            if tableid is None:
                # Table ID not yet assigned
                logger.debug('Cannot get table ID')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            logger.debug('Received table ID:%s', tableid)
            # If ip6tnl is forced, the SID list can only contain one SID
            if (
                storage_helper.is_ip6tnl_forced(deviceid, tenantid)
                and len(
                    self.controller_state_srv6.get_sid_list(
                        deviceid, tenantid, tableid)
                ) > 1
            ):
                logger.error(
                    'force_ip6tnl option is set: Only one SID is supported'
                )
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Third step is the creation of the VRF assigned to the VPN
            response = self.srv6_manager.create_vrf_device(
                deviceip,
                self.grpc_client_port,
                name=overlay_name,
                table=tableid
            )
            if response != SbStatusCode.STATUS_SUCCESS:
                logger.warning(
                    'Cannot create the VRF %s: %s', overlay_name, response
                )
                # If the operation has failed, return an error message
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add reverse action to the rollback stack
            rollback.push(
                func=self.exec_or_mark_device_inconsitent,
                rollback_func=self.srv6_manager.remove_vrf_device,
                server_ip=deviceip,
                server_port=self.grpc_client_port,
                name=overlay_name,
                deviceid=deviceid,
                tenantid=tenantid
            )
            # # Install a blackhole route in the VRF
            # response = self.srv6_manager.create_iproute(
            #     deviceip, self.grpc_client_port, family=AF_INET6,
            #     type='blackhole', table=tableid
            # )
            # if response != SbStatusCode.STATUS_SUCCESS:
            #     logger.warning(
            #         'Cannot create the blackhole route: %s' % response
            #     )
            #     # If the operation has failed, return an error message
            #     return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get a WAN interface
            # We use the WAN interface
            # in order to solve an issue of routes getting deleted when the
            # interface is assigned to a VRF
            dev = storage_helper.get_wan_interfaces(
                deviceid, tenantid
            )
            if dev is None:
                # Cannot get non-loopback interface
                logger.warning('Cannot get non-loopback interface')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            if len(dev) == 0:
                # Cannot get wan interface
                logger.warning(
                    'Cannot get non-loopback interface. No WAN interfaces'
                )
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            dev = dev[0]
            # Get the SID
            logger.debug('Attempting to get a SID for the router')
            sid = self.controller_state_srv6.get_sid(
                deviceid, tenantid, tableid
            )
            logger.debug('Received SID %s' % sid)
            # Add the End.DT4 / End.DT6 route
            response = self.srv6_manager.create_srv6_local_processing_function(
                deviceip,
                self.grpc_client_port,
                segment=sid,
                action=action,
                device=dev,
                localsid_table=srv6_controller_utils.LOCAL_SID_TABLE,
                table=tableid
            )
            if response != SbStatusCode.STATUS_SUCCESS:
                logger.warning(
                    'Cannot create the SRv6 Local Processing function: %s',
                    response
                )
                # The operation has failed, return an error message
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add reverse action to the rollback stack
            rollback.push(
                func=self.exec_or_mark_device_inconsitent,
                rollback_func=(
                    self.srv6_manager.remove_srv6_local_processing_function
                ),
                server_ip=deviceip,
                server_port=self.grpc_client_port,
                segment=sid,
                localsid_table=srv6_controller_utils.LOCAL_SID_TABLE,
                deviceid=deviceid,
                tenantid=tenantid
            )
            # Enable NDP advertisements for the SID
            if (
                storage_helper.is_proxy_ndp_enabled(deviceid, tenantid)
                and storage_helper.get_public_prefix_length(
                    deviceid, tenantid
                ) != 128
            ):
                # Get the WAN interface
                wan_interfaces = storage_helper.get_wan_interfaces(
                    deviceid, tenantid
                )
                if wan_interfaces is None:
                    # Cannot get wan interface
                    logger.warning('Cannot get WAN interface')
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                if len(wan_interfaces) == 0:
                    # Cannot get wan interface
                    logger.warning(
                        'Cannot get WAN interface. No WAN interfaces'
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Get the first WAN interface
                dev = wan_interfaces[0]
                # Enable NDP advertisements for the SID
                response = self.srv6_manager.add_proxy_ndp(
                    deviceip,
                    self.grpc_client_port,
                    address=sid,
                    device=dev,
                    family=AF_INET6
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    # If the operation has failed, return an error message
                    logger.warning('Cannot add proxy NDP: %s', response)
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.del_proxy_ndp,
                    server_ip=deviceip,
                    server_port=self.grpc_client_port,
                    address=sid,
                    device=dev,
                    family=AF_INET6,
                    deviceid=deviceid,
                    tenantid=tenantid
                )
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug(
            'Init overlay completed for the overlay %s and the deviceid %s',
            overlay_name,
            deviceid
        )
        return NbStatusCode.STATUS_OK

    def add_slice_to_overlay(
        self,
        overlayid,
        overlay_name,
        deviceid,
        interface_name,
        tenantid,
        overlay_info
    ):
        logger.debug(
            'Attempting to add the slice %s from the router %s '
            'to the overlay %s',
            interface_name,
            deviceid,
            overlay_name
        )
        with RollbackContext() as rollback:
            # Get router address
            deviceip = storage_helper.get_device_mgmtip(
                tenantid, deviceid
            )
            if deviceip is None:
                # Cannot get the router address
                logger.warning('Cannot get the router address')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Don't advertise the private customer network
            response = self.srv6_manager.update_interface(
                deviceip,
                self.grpc_client_port,
                name=interface_name,
                ospf_adv=False
            )
            if response == SbStatusCode.STATUS_UNREACHABLE_OSPF6D:
                # If the operation has failed, report an error message
                logger.warning(
                    'Cannot disable OSPF advertisements: ospf6d not running'
                )
            elif response != SbStatusCode.STATUS_SUCCESS:
                # If the operation has failed, report an error message
                logger.warning('Cannot disable OSPF advertisements')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add reverse action to the rollback stack
            rollback.push(
                func=self.exec_or_mark_device_inconsitent,
                rollback_func=self.srv6_manager.update_interface,
                server_ip=deviceip,
                server_port=self.grpc_client_port,
                name=interface_name,
                ospf_adv=True,
                deviceid=deviceid,
                tenantid=tenantid
            )
            # Add the interface to the VRF
            response = self.srv6_manager.update_vrf_device(
                deviceip,
                self.grpc_client_port,
                name=overlay_name,
                interfaces=[interface_name],
                op='add_interfaces'
            )
            if response != SbStatusCode.STATUS_SUCCESS:
                # If the operation has failed, report an error message
                logger.warning(
                    'Cannot assign the interface to the VRF: %s', response
                )
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add reverse action to the rollback stack
            rollback.push(
                func=self.exec_or_mark_device_inconsitent,
                rollback_func=self.srv6_manager.update_vrf_device,
                server_ip=deviceip,
                server_port=self.grpc_client_port,
                name=overlay_name,
                interfaces=[interface_name],
                op='del_interfaces',
                deviceid=deviceid,
                tenantid=tenantid
            )
            # Get the table ID
            tableid = storage_helper.get_tableid(overlayid, tenantid)
            if tableid is None:
                logger.warning('Cannot retrieve VPN table ID')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Create the routes for the subnets
            subnets = storage_helper.get_ip_subnets(
                deviceid, tenantid, interface_name
            )
            for subnet in subnets:
                gateway = subnet['gateway']
                subnet = subnet['subnet']
                if gateway is not None and gateway != '':
                    response = self.srv6_manager.create_iproute(
                        deviceip,
                        self.grpc_client_port,
                        destination=subnet,
                        gateway=gateway,
                        out_interface=interface_name,
                        table=tableid
                    )
                    if response != SbStatusCode.STATUS_SUCCESS:
                        # If the operation has failed, report an error message
                        logger.warning(
                            'Cannot set route for %s (gateway %s) in %s ',
                            subnet,
                            gateway,
                            deviceip
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=self.srv6_manager.remove_iproute,
                        server_ip=deviceip,
                        server_port=self.grpc_client_port,
                        destination=subnet,
                        gateway=gateway,
                        table=tableid,
                        deviceid=deviceid,
                        tenantid=tenantid
                    )
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug('Add slice to overlay completed')
        return NbStatusCode.STATUS_OK

    def create_tunnel(
        self,
        overlayid,
        overlay_name,
        overlay_type,
        l_slice,
        r_slice,
        tenantid,
        overlay_info
    ):
        logger.debug(
            'Attempting to create a tunnel %s between the interfaces %s '
            'and %s',
            overlay_name,
            l_slice['interface_name'],
            r_slice['interface_name']
        )
        with RollbackContext() as rollback:
            # Tunnel from l_slice to r_slice
            res = self._create_tunnel_uni(
                overlayid,
                overlay_name,
                overlay_type,
                l_slice,
                r_slice,
                tenantid,
                overlay_info
            )
            if res != NbStatusCode.STATUS_OK:
                return res
            # Add reverse action to the rollback stack
            rollback.push(
                func=self._remove_tunnel_uni,
                overlayid=overlayid,
                overlay_name=overlay_name,
                overlay_type=overlay_type,
                l_slice=l_slice,
                r_slice=r_slice,
                tenantid=tenantid,
                overlay_info=overlay_info
            )
            # Tunnel from r_slice to l_slice
            res = self._create_tunnel_uni(
                overlayid, overlay_name,
                overlay_type,
                r_slice, l_slice,
                tenantid,
                overlay_info
            )
            if res != NbStatusCode.STATUS_OK:
                return res
            # Add reverse action to the rollback stack
            rollback.push(
                func=self._remove_tunnel_uni,
                overlayid=overlayid,
                overlay_name=overlay_name,
                overlay_type=overlay_type,
                l_slice=r_slice,
                r_slice=l_slice,
                tenantid=tenantid,
                overlay_info=overlay_info
            )
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug('Tunnel creation completed')
        return NbStatusCode.STATUS_OK

    def destroy_overlay_data(
        self,
        overlayid,
        overlay_name,
        tenantid,
        overlay_info,
        ignore_errors=False
    ):
        logger.debug('Trying to destroy the overlay data structure')
        with RollbackContext() as rollback:
            # Release the table ID
            res = storage_helper.release_tableid(
                overlayid, tenantid)
            if res == -1:
                logger.debug('Cannot release the table ID')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add reverse action to the rollback stack
            rollback.push(
                func=storage_helper.get_new_tableid,
                overlayid=overlayid,
                tenantid=tenantid
            )
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug('Destroy overlay data completed')
        return NbStatusCode.STATUS_OK

    def destroy_tunnel_mode(
        self,
        deviceid,
        tenantid,
        overlay_info,
        ignore_errors=False
    ):
        logger.debug(
            'Trying to destroy the tunnel mode on the router %s', deviceid
        )
        with RollbackContext() as rollback:
            # Get router address
            deviceip = storage_helper.get_router_mgmtip(
                deviceid, tenantid
            )
            if deviceip is None:
                # Cannot get the router address
                logger.warning('Cannot get the router address')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get SID family for this router
            sid_family = self.controller_state_srv6.get_sid_family(
                deviceid, tenantid
            )
            if sid_family is None:
                # If the operation has failed, return an error message
                logger.warning(
                    'Cannot get SID family for deviceid %s', deviceid
                )
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Remove rule for SIDs
            # The IP rule is present only if the main routing table is not used
            # as local SID table
            if srv6_controller_utils.LOCAL_SID_TABLE != \
                    srv6_controller_utils.MAIN_ROUTING_TABLE:
                response = self.srv6_manager.remove_iprule(
                    deviceip,
                    self.grpc_client_port,
                    family=AF_INET6,
                    table=srv6_controller_utils.LOCAL_SID_TABLE,
                    destination=sid_family
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    if ignore_errors:
                        # Change device state to reboot required
                        success = storage_helper.change_device_state(
                            deviceid=deviceid,
                            tenantid=tenantid,
                            new_state=DeviceState.REBOOT_REQUIRED
                        )
                        if success is False or success is None:
                            logging.error('Error changing the device state')
                            return status_codes_pb2.STATUS_INTERNAL_ERROR
                    else:
                        # If the operation has failed, return an error message
                        logger.warning(
                            'Cannot remove the localSID rule: %s', response
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.create_iprule,
                    server_ip=deviceip,
                    server_port=self.grpc_client_port,
                    family=AF_INET6,
                    table=srv6_controller_utils.LOCAL_SID_TABLE,
                    destination=sid_family,
                    deviceid=deviceid,
                    tenantid=tenantid
                )
            # Remove blackhole route
            # The blackhole route is present only if the main routing table is
            # not used as local SID table
            if srv6_controller_utils.LOCAL_SID_TABLE != \
                    srv6_controller_utils.MAIN_ROUTING_TABLE:
                response = self.srv6_manager.remove_iproute(
                    deviceip,
                    self.grpc_client_port,
                    family=AF_INET6,
                    type='blackhole',
                    table=srv6_controller_utils.LOCAL_SID_TABLE
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    if ignore_errors:
                        # Change device state to reboot required
                        success = storage_helper.change_device_state(
                            deviceid=deviceid,
                            tenantid=tenantid,
                            new_state=DeviceState.REBOOT_REQUIRED
                        )
                        if success is False or success is None:
                            logging.error('Error changing the device state')
                            return status_codes_pb2.STATUS_INTERNAL_ERROR
                    else:
                        # If the operation has failed, return an error message
                        logger.warning(
                            'Cannot remove the blackhole rule: %s', response
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.create_iproute,
                    server_ip=deviceip,
                    server_port=self.grpc_client_port,
                    family=AF_INET6,
                    type='blackhole',
                    table=srv6_controller_utils.LOCAL_SID_TABLE,
                    deviceid=deviceid,
                    tenantid=tenantid
                )
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug('Destroy tunnel mode completed')
        return NbStatusCode.STATUS_OK

    def destroy_overlay(
        self,
        overlayid,
        overlay_name,
        overlay_type,
        tenantid,
        deviceid,
        overlay_info,
        ignore_errors=False
    ):
        logger.debug(
            'Trying to destroy the overlay %s on device %s',
            overlay_name,
            deviceid
        )
        with RollbackContext() as rollback:
            # Get the router address
            deviceip = storage_helper.get_router_mgmtip(
                deviceid, tenantid
            )
            if deviceip is None:
                # Cannot get the router address
                logger.warning('Cannot get the router address')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Extract params from the VPN
            tableid = storage_helper.get_tableid(
                overlayid, tenantid
            )
            if tableid is None:
                # If the operation has failed, return an error message
                logger.warning(
                    'Cannot get table ID for the VPN %s', overlay_name
                )
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get the SID
            sid = self.controller_state_srv6.get_sid(
                deviceid, tenantid, tableid
            )
            if sid is None:
                # If the operation has failed, return an error message
                logger.warning('Cannot get SID for deviceid %s', deviceid)
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Disable NDP advertisements for the SID
            if (
                storage_helper.is_proxy_ndp_enabled(deviceid, tenantid)
                and storage_helper.get_public_prefix_length(
                    deviceid, tenantid
                ) != 128
            ):
                # Get the WAN interface
                wan_interfaces = storage_helper.get_wan_interfaces(
                    deviceid, tenantid
                )
                if wan_interfaces is None:
                    # Cannot get wan interface
                    logger.warning('Cannot get WAN interface')
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                if len(wan_interfaces) == 0:
                    # Cannot get wan interface
                    logger.warning(
                        'Cannot get WAN interface. No WAN interfaces'
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Get the first WAN interface
                dev = wan_interfaces[0]
                # Disable NDP advertisements for the SID
                response = self.srv6_manager.del_proxy_ndp(
                    deviceip,
                    self.grpc_client_port,
                    address=sid,
                    device=dev,
                    family=AF_INET6
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    if ignore_errors:
                        # Change device state to reboot required
                        success = storage_helper.change_device_state(
                            deviceid=deviceid,
                            tenantid=tenantid,
                            new_state=DeviceState.REBOOT_REQUIRED
                        )
                        if success is False or success is None:
                            logging.error('Error changing the device state')
                            return status_codes_pb2.STATUS_INTERNAL_ERROR
                    else:
                        # If the operation has failed, return an error message
                        logger.warning('Cannot remove proxy NDP: %s', response)
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.add_proxy_ndp,
                    server_ip=deviceip,
                    server_port=self.grpc_client_port,
                    address=sid,
                    device=dev,
                    family=AF_INET6,
                    deviceid=deviceid,
                    tenantid=tenantid
                )
            # Remove the decap and lookup function (i.e. the End.DT4 or End.DT6
            # route)
            response = self.srv6_manager.remove_srv6_local_processing_function(
                deviceip, self.grpc_client_port, segment=sid,
                localsid_table=srv6_controller_utils.LOCAL_SID_TABLE
            )
            if response != SbStatusCode.STATUS_SUCCESS:
                if ignore_errors:
                    # Change device state to reboot required
                    success = storage_helper.change_device_state(
                        deviceid=deviceid,
                        tenantid=tenantid,
                        new_state=DeviceState.REBOOT_REQUIRED
                    )
                    if success is False or success is None:
                        logging.error('Error changing the device state')
                        return status_codes_pb2.STATUS_INTERNAL_ERROR
                else:
                    # If the operation has failed, return an error message
                    logger.warning(
                        'Cannot remove seg6local route: %s', response
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get action
            if overlay_type == 'IPv6Overlay':
                # For IPv6 VPN we have to perform decap and lookup in IPv6
                # routing table. This behavior is realized by End.DT6 SRv6
                # action
                action = 'End.DT6'
            elif overlay_type == 'IPv4Overlay':
                # For IPv4 VPN we have to perform decap and lookup in IPv6
                # routing table. This behavior is realized by End.DT4 SRv6
                # action
                action = 'End.DT4'
            else:
                logger.warning(
                    'Error: Unsupported VPN type: %s', overlay_type
                )
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get the WAN interface
            wan_interfaces = storage_helper.get_wan_interfaces(
                deviceid, tenantid
            )
            if wan_interfaces is None:
                # Cannot get wan interface
                logger.warning('Cannot get WAN interface')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            if len(wan_interfaces) == 0:
                # Cannot get wan interface
                logger.warning('Cannot get WAN interface. No WAN interfaces')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get the first WAN interface
            dev = wan_interfaces[0]
            # Add reverse action to the rollback stack
            rollback.push(
                func=self.exec_or_mark_device_inconsitent,
                rollback_func=(
                    self.srv6_manager.create_srv6_local_processing_function
                ),
                server_ip=deviceip,
                server_port=self.grpc_client_port,
                segment=sid,
                action=action,
                device=dev,
                localsid_table=srv6_controller_utils.LOCAL_SID_TABLE,
                table=tableid,
                deviceid=deviceid,
                tenantid=tenantid
            )
            # Delete the VRF assigned to the VPN
            response = self.srv6_manager.remove_vrf_device(
                deviceip, self.grpc_client_port, overlay_name
            )
            if response != SbStatusCode.STATUS_SUCCESS:
                if ignore_errors:
                    # Change device state to reboot required
                    success = storage_helper.change_device_state(
                        deviceid=deviceid,
                        tenantid=tenantid,
                        new_state=DeviceState.REBOOT_REQUIRED
                    )
                    if success is False or success is None:
                        logging.error('Error changing the device state')
                        return status_codes_pb2.STATUS_INTERNAL_ERROR
                else:
                    # If the operation has failed, return an error message
                    logger.warning(
                        'Cannot remove the VRF %s from the router %s: %s',
                        overlay_name,
                        deviceid,
                        response
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add reverse action to the rollback stack
            rollback.push(
                func=self.exec_or_mark_device_inconsitent,
                rollback_func=self.srv6_manager.create_vrf_device,
                server_ip=deviceip,
                server_port=self.grpc_client_port,
                name=overlay_name,
                table=tableid,
                deviceid=deviceid,
                tenantid=tenantid
            )
            # Delete all remaining IPv6 routes associated to the VPN
            response = self.srv6_manager.remove_iproute(
                deviceip, self.grpc_client_port, family=AF_INET6, table=tableid
            )
            if response != SbStatusCode.STATUS_SUCCESS:
                if ignore_errors:
                    # Change device state to reboot required
                    success = storage_helper.change_device_state(
                        deviceid=deviceid,
                        tenantid=tenantid,
                        new_state=DeviceState.REBOOT_REQUIRED
                    )
                    if success is False or success is None:
                        logging.error('Error changing the device state')
                        return status_codes_pb2.STATUS_INTERNAL_ERROR
                else:
                    # If the operation has failed, return an error message
                    logger.warning(
                        'Cannot remove the IPv6 route: %s', response
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # TODO how can we undo this?
            # Delete all remaining IPv4 routes associated to the VPN
            response = self.srv6_manager.remove_iproute(
                deviceip, self.grpc_client_port, family=AF_INET, table=tableid
            )
            if response != SbStatusCode.STATUS_SUCCESS:
                if ignore_errors:
                    # Change device state to reboot required
                    success = storage_helper.change_device_state(
                        deviceid=deviceid,
                        tenantid=tenantid,
                        new_state=DeviceState.REBOOT_REQUIRED
                    )
                    if success is False or success is None:
                        logging.error('Error changing the device state')
                        return status_codes_pb2.STATUS_INTERNAL_ERROR
                else:
                    # If the operation has failed, return an error message
                    logger.warning('Cannot remove IPv4 routes: %s', response)
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # TODO how can we undo this?
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug('Destroy overlay completed')
        return NbStatusCode.STATUS_OK

    def remove_slice_from_overlay(
        self,
        overlayid,
        overlay_name,
        deviceid,
        interface_name,
        tenantid,
        overlay_info,
        ignore_errors=False
    ):
        logger.debug(
            'Trying to remove the slice %s on device %s '
            'from the overlay %s',
            interface_name,
            deviceid,
            overlay_name
        )
        with RollbackContext() as rollback:
            # Get router address
            deviceip = storage_helper.get_router_mgmtip(
                deviceid, tenantid
            )
            if deviceip is None:
                # Cannot get the router address
                logger.warning('Cannot get the router address')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get the table ID
            tableid = storage_helper.get_tableid(
                overlayid, tenantid
            )
            if tableid is None:
                logger.warning('Cannot retrieve VPN table ID')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Remove IP routes from the VRF
            # This step is optional, because the routes are
            # automatically removed when the interfaces is removed
            # from the VRF. We do it just for symmetry with respect
            # to the add_slice_to_overlay function
            subnets = storage_helper.get_ip_subnets(
                deviceid, tenantid, interface_name
            )
            for subnet in subnets:
                gateway = subnet['gateway']
                subnet = subnet['subnet']
                if gateway is not None and gateway != '':
                    response = self.srv6_manager.remove_iproute(
                        deviceip,
                        self.grpc_client_port,
                        destination=subnet,
                        gateway=gateway,
                        table=tableid
                    )
                    if response != SbStatusCode.STATUS_SUCCESS:
                        if ignore_errors:
                            # Change device state to reboot required
                            success = storage_helper.change_device_state(
                                deviceid=deviceid,
                                tenantid=tenantid,
                                new_state=DeviceState.REBOOT_REQUIRED
                            )
                            if success is False or success is None:
                                logging.error(
                                    'Error changing the device state'
                                )
                                return status_codes_pb2.STATUS_INTERNAL_ERROR
                        else:
                            # If the operation has failed, report an error
                            # message
                            logger.warning(
                                'Cannot remove route for %s (gateway %s) '
                                'in %s ',
                                subnet,
                                gateway,
                                deviceip
                            )
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=self.srv6_manager.create_iproute,
                        server_ip=deviceip,
                        server_port=self.grpc_client_port,
                        destination=subnet,
                        gateway=gateway,
                        out_interface=interface_name,
                        table=tableid,
                        deviceid=deviceid,
                        tenantid=tenantid
                    )
            # Enable advertisements the private customer network
            response = self.srv6_manager.update_interface(
                deviceip,
                self.grpc_client_port,
                name=interface_name,
                ospf_adv=True
            )
            if response == SbStatusCode.STATUS_UNREACHABLE_OSPF6D:
                # If the operation has failed, report an error message
                logger.warning(
                    'Cannot disable OSPF advertisements: ospf6d not running'
                )
            elif response != SbStatusCode.STATUS_SUCCESS:
                if ignore_errors:
                    # Change device state to reboot required
                    success = storage_helper.change_device_state(
                        deviceid=deviceid,
                        tenantid=tenantid,
                        new_state=DeviceState.REBOOT_REQUIRED
                    )
                    if success is False or success is None:
                        logging.error('Error changing the device state')
                        return status_codes_pb2.STATUS_INTERNAL_ERROR
                else:
                    # If the operation has failed, return an error message
                    logger.warning(
                        'Cannot enable OSPF advertisements: %s', response
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add reverse action to the rollback stack
            rollback.push(
                func=self.exec_or_mark_device_inconsitent,
                rollback_func=self.srv6_manager.update_vrf_device,
                server_ip=deviceip,
                server_port=self.grpc_client_port,
                name=interface_name,
                ospf_adv=False,
                deviceid=deviceid,
                tenantid=tenantid
            )
            # Remove the interface from the VRF
            response = self.srv6_manager.update_vrf_device(
                deviceip,
                self.grpc_client_port,
                name=overlay_name,
                interfaces=[interface_name],
                op='del_interfaces'
            )
            if response != SbStatusCode.STATUS_SUCCESS:
                if ignore_errors:
                    # Change device state to reboot required
                    success = storage_helper.change_device_state(
                        deviceid=deviceid,
                        tenantid=tenantid,
                        new_state=DeviceState.REBOOT_REQUIRED
                    )
                    if success is False or success is None:
                        logging.error('Error changing the device state')
                        return status_codes_pb2.STATUS_INTERNAL_ERROR
                else:
                    # If the operation has failed, return an error message
                    logger.warning(
                        'Cannot remove the VRF device: %s', response
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add reverse action to the rollback stack
            rollback.push(
                func=self.exec_or_mark_device_inconsitent,
                rollback_func=self.srv6_manager.update_vrf_device,
                server_ip=deviceip,
                server_port=self.grpc_client_port,
                name=overlay_name,
                interfaces=[interface_name],
                op='add_interfaces',
                deviceid=deviceid,
                tenantid=tenantid
            )
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug('Remove slice from overlay completed')
        return NbStatusCode.STATUS_OK

    def remove_tunnel(
        self,
        overlayid,
        overlay_name,
        overlay_type,
        l_slice,
        r_slice,
        tenantid,
        overlay_info,
        ignore_errors=False
    ):
        logger.debug(
            'Attempting to remove the tunnel %s between the interfaces '
            '%s and %s',
            overlay_name,
            l_slice['interface_name'],
            r_slice['interface_name']
        )
        with RollbackContext() as rollback:
            # Tunnel from l_slice to r_slice
            res = self._remove_tunnel_uni(
                overlayid, overlay_name,
                overlay_type,
                l_slice,
                r_slice,
                tenantid,
                overlay_info,
                ignore_errors=ignore_errors
            )
            if res != NbStatusCode.STATUS_OK:
                return res
            # Add reverse action to the rollback stack
            rollback.push(
                func=self._create_tunnel_uni,
                overlayid=overlayid,
                overlay_name=overlay_name,
                overlay_type=overlay_type,
                l_slice=l_slice,
                r_slice=r_slice,
                tenantid=tenantid,
                overlay_info=overlay_info
            )
            # Tunnel from r_slice to l_slice
            res = self._remove_tunnel_uni(
                overlayid, overlay_name,
                overlay_type,
                r_slice,
                l_slice,
                tenantid,
                overlay_info,
                ignore_errors=ignore_errors
            )
            if res != NbStatusCode.STATUS_OK:
                return res
            # Add reverse action to the rollback stack
            rollback.push(
                func=self._create_tunnel_uni,
                overlayid=overlayid,
                overlay_name=overlay_name,
                overlay_type=overlay_type,
                l_slice=r_slice,
                r_slice=l_slice,
                tenantid=tenantid,
                overlay_info=overlay_info
            )
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        # Success
        logger.debug('Remove tunnel completed')
        return NbStatusCode.STATUS_OK

    def get_sid_lists(
        self,
        ingress_deviceid,
        egress_deviceid,
        tenantid
    ):
        # Get all the overlays common to the two devices
        overlays = storage_helper.get_overlays_containing_devices(
            deviceid1=ingress_deviceid,
            deviceid2=egress_deviceid,
            tenantid=tenantid
        )
        if overlays is None:
            err = 'Error getting overlays containing devices'
            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR, err, None
        # Get the SID list (in both the directions) between the two devices
        # for each overlay
        sid_lists = []
        for overlay in overlays:
            if overlay['tunnel_mode'] != 'SRv6':
                # We are only interested in SRv6 tunnels
                # Skip non-SRv6 tunnels
                continue
            # Retrieve the SID list
            _overlayid = str(overlay['_id'])
            _overlay_name = overlay['name']
            _tenantid = overlay['tenantid']
            _tableid = storage_helper.get_tableid(
                overlayid=_overlayid, tenantid=_tenantid
            )
            if _tableid is None:
                # If the operation has failed, return an error message
                err = 'Cannot get table ID for the VPN %s' % _overlay_name
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR, err, None
            _direct_sid_list = self.controller_state_srv6.get_sid_list(
                deviceid=egress_deviceid, tenantid=_tenantid, tableid=_tableid
            )
            _return_sid_list = self.controller_state_srv6.get_sid_list(
                deviceid=ingress_deviceid, tenantid=_tenantid, tableid=_tableid
            )
            # Add the SID list
            sid_lists.append({
                'overlayid': _overlayid,
                'overlay_name': _overlay_name,
                'direct_sid_list': _direct_sid_list,
                'return_sid_list':  _return_sid_list,
                'tenantid': _tenantid
            })
        # Return the result
        return NbStatusCode.STATUS_OK, 'OK', sid_lists

    # def get_overlays(self):
    #     # Create the response
    #     response = srv6_vpn_pb2.SRv6VPNReply(
    #         status=SbStatusCode.STATUS_SUCCESS)
    #     # Build the VPNs list
    #     for _vpn in self.controller_state_srv6.controller_state.get_vpns():
    #         # Add a new VPN to the VPNs list
    #         vpn = response.vpns.add()
    #         # Set name
    #         vpn.overlay_name = _vpn.overlay_name
    #         # Set table ID
    #         vpn.tableid = _vpn.tableid
    #         # Set interfaces
    #         # Iterate on all interfaces
    #         for interfaces in _vpn.interfaces.values():
    #             for interface in interfaces.values():
    #                 # Add a new interface to the VPN
    #                 _interface = vpn.interfaces.add()
    #                 # Add router ID
    #                 _interface.routerid = interface.routerid
    #                 # Add interface name
    #                 _interface.interface_name = interface.interface_name
    #                 # Add interface IP
    #                 _interface.interface_ip = interface.interface_ip
    #                 # Add VPN prefix
    #                 _interface.subnets = interface.subnets
    #     # Return the VPNs list
    #     logger.debug('Sending response:\n%s' % response)
    #     return response

    def _create_tunnel_uni_reconciliation_l(
        self,
        overlayid,
        overlay_name,
        overlay_type,
        l_slice,
        r_slice,
        tenantid,
        overlay_info
    ):
        logger.debug(
            'Attempting to create unidirectional tunnel from %s to %s',
            l_slice['interface_name'], r_slice['interface_name']
        )
        with RollbackContext() as rollback:
            # Check if the unidirectional tunnel
            # between the two slices already exists
            #
            # Increase the number of tunnels
            num_tunnels = storage_helper.inc_and_get_tunnels_counter(
                overlayid, tenantid, l_slice['deviceid'], r_slice
            )
            # Add reverse action to the rollback stack
            rollback.push(
                func=storage_helper.dec_and_get_tunnels_counter,
                overlayid=overlayid,
                tenantid=tenantid,
                deviceid=l_slice['deviceid'],
                dest_slice=r_slice
            )
            # If the uni tunnel already exists, we have done
            if num_tunnels is not None and num_tunnels > 1:
                logger.debug(
                    'Skip tunnel %s %s',
                    l_slice['interface_name'],
                    r_slice['interface_name']
                )
                # Success, commit all performed operations
                rollback.commitAll()
                return NbStatusCode.STATUS_OK
            # Configure the tunnel
            #
            # Get router address
            l_deviceip = storage_helper.get_router_mgmtip(
                l_slice['deviceid'], tenantid
            )
            if l_deviceip is None:
                # Cannot get the router address
                logger.warning('Cannot get the router address')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Any non-loopback device
            # We use the WAN interface
            # in order to solve an issue of routes getting deleted when the
            # interface is assigned to a VRF
            dev = storage_helper.get_wan_interfaces(
                l_slice['deviceid'], tenantid
            )
            if dev is None:
                # Cannot get wan interface
                logger.warning('Cannot get WAN interface')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            if len(dev) == 0:
                # Cannot get wan interface
                logger.warning('Cannot get WAN interface. No WAN interfaces')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            dev = dev[0]
            # Get the table ID
            tableid = storage_helper.get_tableid(
                overlayid, tenantid
            )
            if tableid is None:
                logger.warning('Cannot retrieve VPN table ID')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get the SID
            sid_list = self.controller_state_srv6.get_sid_list(
                r_slice['deviceid'], tenantid, tableid
            )
            # pyroute2 requires SID list in reverse order
            sid_list = sid_list[::-1]
            # Get the subnets
            if overlay_type == OverlayType.IPv6Overlay:
                subnets = storage_helper.get_ipv6_subnets(
                    r_slice['deviceid'], tenantid, r_slice['interface_name']
                )
            elif overlay_type == OverlayType.IPv4Overlay:
                subnets = storage_helper.get_ipv4_subnets(
                    r_slice['deviceid'], tenantid, r_slice['interface_name']
                )
            else:
                logger.warning('Error: Unsupported VPN type: %s', overlay_type)
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add the tunnel to the controller state and get the tunnel ID of
            # the new tunnel
            tunnel = storage_helper.get_tunnel(
                overlayid=overlayid,
                ldeviceid=l_slice['deviceid'],
                rdeviceid=r_slice['deviceid'],
                tenantid=tenantid
            )
            if tunnel is None:
                logger.warning('Error: Cannot store tunnel')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # If the SID list has only one SID, we can store the SID in the
            # IPv6 destination address field of the packets; in this case, the
            # SRH is not required and the encapsulation becomes an IPv4 over
            # IPv6 encapsulation or IPv6 over IPv6 encapsulation
            if (
                not storage_helper.is_srh_forced(r_slice['deviceid'], tenantid)
                and len(sid_list) == 1
            ):
                # Create a name for the Linux interface and establish the
                # tunnel type
                ip6tnl_ifname = tunnel['tunnel_name']
                ip6tnl_tx_ifname = '%s-%s-tx' % (ip6tnl_ifname, tableid)
                # ip6tnl_rx_ifname = '%s-%s-rx' % (ip6tnl_ifname, tableid)
                if overlay_type == OverlayType.IPv4Overlay:
                    tunnel_type = 'ip4ip6'
                elif overlay_type == OverlayType.IPv6Overlay:
                    tunnel_type = 'ip6ip6'
                else:
                    logger.warning(
                        'Error: Unsupported VPN type: %s', overlay_type
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Create an ip6tnl Linux interface to encapsulate the traffic
                # sent over the tunnel
                # Get a WAN interface
                wan_interfaces = storage_helper.get_wan_interfaces(
                    l_slice['deviceid'], tenantid
                )
                if wan_interfaces is None:
                    # Cannot get WAN interface
                    logger.warning('Cannot get WAN interfaces')
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                if len(wan_interfaces) == 0:
                    # Cannot get WAN interface
                    logger.warning('Cannot get WAN interface')
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                wan_interface = wan_interfaces[0]
                # Get an IPv6 address
                addrs = storage_helper.get_ipv6_addresses(
                    deviceid=l_slice['deviceid'],
                    tenantid=tenantid,
                    interface_name=wan_interface
                )
                if len(addrs) == 0:
                    # No IPv6 address assigned to the interface
                    logging.error(
                        'No IPv6 addresses assigned to the interface %s',
                        wan_interface
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                l_device_wan_ipaddr = addrs[0]
                # Create the ip6tnl interface
                response = self.srv6_manager.create_ip_tunnel_interface(
                    server_ip=l_deviceip,
                    server_port=self.grpc_client_port,
                    ifname=ip6tnl_tx_ifname,
                    local_addr=l_device_wan_ipaddr.split('/')[0],
                    remote_addr=sid_list[0],
                    tunnel_type=tunnel_type
                )
                if response == SbStatusCode.STATUS_FILE_EXISTS:
                    logger.warning(
                        'Cannot create the IP Tunnel interface. Tunnel '
                        'already exists. Skipping'
                    )
                elif response != SbStatusCode.STATUS_SUCCESS:
                    logger.warning(
                        'Cannot create the IP Tunnel interface: %s', response
                    )
                    # The operation has failed, return an error message
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.remove_ip_tunnel_interface,
                    server_ip=l_deviceip,
                    server_port=self.grpc_client_port,
                    ifname=ip6tnl_tx_ifname,
                    deviceid=l_slice['deviceid'],
                    tenantid=tenantid
                )
                # Add the tunnel interface to the VRF
                response = self.srv6_manager.update_vrf_device(
                    l_deviceip,
                    self.grpc_client_port,
                    name=overlay_name,
                    interfaces=[ip6tnl_tx_ifname],
                    op='add_interfaces'
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    # If the operation has failed, report an error message
                    logger.warning(
                        'Cannot assign the interface to the VRF: %s', response
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.update_vrf_device,
                    server_ip=l_deviceip,
                    server_port=self.grpc_client_port,
                    name=overlay_name,
                    interfaces=[ip6tnl_tx_ifname],
                    op='del_interfaces',
                    deviceid=l_slice['deviceid'],
                    tenantid=tenantid
                )
                # Set the IP address
                response = self.srv6_manager.create_ipaddr(
                    l_deviceip,
                    self.grpc_client_port,
                    ip_addr=l_device_wan_ipaddr,
                    device=ip6tnl_tx_ifname,
                    family=AF_INET6
                )
                if response == SbStatusCode.STATUS_FILE_EXISTS:
                    logger.warning(
                        'Cannot assign the IP address to the tunnel '
                        'interface. Address already exists. Skipping'
                    )
                elif response != SbStatusCode.STATUS_SUCCESS:
                    # If the operation has failed,
                    # report an error message
                    logging.warning(
                        'Cannot assign the IP address to the tunnel interface'
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.remove_ipaddr,
                    server_ip=l_deviceip,
                    server_port=self.grpc_client_port,
                    ip_addr=l_device_wan_ipaddr,
                    device=ip6tnl_tx_ifname,
                    family=AF_INET6,
                    deviceid=l_slice['deviceid'],
                    tenantid=tenantid
                )
            if (
                len(sid_list) > 1
                and (
                    storage_helper.get_outgoing_sr_transparency(
                        l_slice['deviceid'], tenantid
                    ) == 't1'
                    or storage_helper.get_incoming_sr_transparency(
                        r_slice['deviceid'], tenantid
                    ) == 't1'
                )
            ):
                # SID list > 1 and Transparency T1, double encap is required
                # to create the tunnel (IP over IPv6+SRH over IPv6)
                first_tunnel = True  # FIXME
                if first_tunnel:
                    # Create a name for the Linux interface and establish the
                    # tunnel type
                    ip6tnl_ifname = tunnel['tunnel_name']
                    ip6tnl_tx_ifname = '%s-%s-tx' % (ip6tnl_ifname, tableid)
                    # ip6tnl_rx_ifname = '%s-%s-rx' % (ip6tnl_ifname, tableid)
                    # Create an ip6tnl Linux interface to encapsulate the
                    # traffic sent over the tunnel
                    # Get a WAN interface
                    wan_interfaces = storage_helper.get_wan_interfaces(
                        l_slice['deviceid'], tenantid
                    )
                    if wan_interfaces is None:
                        # Cannot get WAN interface
                        logger.warning('Cannot get WAN interfaces')
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    if len(wan_interfaces) == 0:
                        # Cannot get WAN interface
                        logger.warning('Cannot get WAN interface')
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    wan_interface = wan_interfaces[0]
                    # Get an IPv6 address
                    addrs = storage_helper.get_ipv6_addresses(
                        deviceid=l_slice['deviceid'],
                        tenantid=tenantid,
                        interface_name=wan_interface
                    )
                    if len(addrs) == 0:
                        # No IPv6 address assigned to the interface
                        logging.error(
                            'No IPv6 addresses assigned to the interface %s',
                            wan_interface
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    l_device_wan_ipaddr = addrs[0]
                    # Create the ip6tnl interface
                    response = self.srv6_manager.create_ip_tunnel_interface(
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        ifname=ip6tnl_tx_ifname,
                        local_addr=l_device_wan_ipaddr.split('/')[0],
                        remote_addr=sid_list[-1],
                        tunnel_type='ip6ip6'
                    )
                    if response == SbStatusCode.STATUS_FILE_EXISTS:
                        logger.warning(
                            'Cannot create the tunnel interface. Tunnel '
                            'already exists. Skipping'
                        )
                    elif response != SbStatusCode.STATUS_SUCCESS:
                        logger.warning(
                            'Cannot create the IP Tunnel interface: %s',
                            response
                        )
                        # The operation has failed, return an error message
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=(
                            self.srv6_manager.remove_ip_tunnel_interface
                        ),
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        ifname=ip6tnl_tx_ifname,
                        deviceid=l_slice['deviceid'],
                        tenantid=tenantid
                    )
                    # Add the tunnel interface to the VRF
                    response = self.srv6_manager.update_vrf_device(
                        l_deviceip,
                        self.grpc_client_port,
                        name=overlay_name,
                        interfaces=[ip6tnl_tx_ifname],
                        op='add_interfaces'
                    )
                    if response != SbStatusCode.STATUS_SUCCESS:
                        # If the operation has failed, report an error message
                        logger.warning(
                            'Cannot assign the interface to the VRF: %s',
                            response
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=self.srv6_manager.update_vrf_device,
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        name=overlay_name,
                        interfaces=[ip6tnl_tx_ifname],
                        op='del_interfaces',
                        deviceid=l_slice['deviceid'],
                        tenantid=tenantid
                    )
                    # Set the IP address
                    response = self.srv6_manager.create_ipaddr(
                        l_deviceip,
                        self.grpc_client_port,
                        ip_addr='2001:db9::1/64',
                        device=ip6tnl_tx_ifname,
                        family=AF_INET6
                    )
                    if response == SbStatusCode.STATUS_FILE_EXISTS:
                        logger.warning(
                            'Cannot assign the IP address to the tunnel '
                            'interface. Address already exists. Skipping'
                        )
                    elif response != SbStatusCode.STATUS_SUCCESS:
                        # If the operation has failed,
                        # report an error message
                        logging.warning(
                            'Cannot assign the IP address to the tunnel '
                            'interface'
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=self.srv6_manager.remove_ipaddr,
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        ip_addr='2001:db9::1/64',
                        device=ip6tnl_tx_ifname,
                        family=AF_INET6,
                        deviceid=l_slice['deviceid'],
                        tenantid=tenantid
                    )
            # Create the SRv6 route
            for subnet in subnets:
                subnet = subnet['subnet']
                if len(sid_list) == 1:
                    # If we are using an IP over IPv6 encapsulation, we need to
                    # redirect the traffic of the slice over the ip6tnl tunnel
                    response = self.srv6_manager.create_iproute(
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        destination=subnet,
                        out_interface=ip6tnl_tx_ifname,
                        table=tableid
                    )
                    if response == SbStatusCode.STATUS_FILE_EXISTS:
                        logger.warning(
                            'Cannot set the route. Route already exists. '
                            'Skipping'
                        )
                    elif response != SbStatusCode.STATUS_SUCCESS:
                        # If the operation has failed, report an error message
                        logger.warning(
                            'Cannot set route for %s in %s ',
                            subnet,
                            l_deviceip
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=self.srv6_manager.remove_iproute,
                        server_ip=l_deviceip,
                        server_port=self.grpc_client_port,
                        destination=subnet,
                        out_interface=ip6tnl_tx_ifname,
                        table=tableid,
                        deviceid=l_slice['deviceid'],
                        tenantid=tenantid
                    )
                else:
                    if (
                        storage_helper.get_outgoing_sr_transparency(
                            l_slice['deviceid'], tenantid
                        ) == 't1'
                        or storage_helper.get_incoming_sr_transparency(
                            r_slice['deviceid'], tenantid
                        ) == 't1'
                    ):
                        response = self.srv6_manager.create_srv6_explicit_path(
                            l_deviceip,
                            self.grpc_client_port,
                            destination=subnet,
                            table=tableid,
                            device=dev,
                            segments=sid_list[:-1],
                            encapmode='encap'
                        )
                        if response == SbStatusCode.STATUS_FILE_EXISTS:
                            logger.warning(
                               'Cannot create the SRv6 Explicit Path. Path '
                               'already exists. Skipping'
                            )
                        elif response != SbStatusCode.STATUS_SUCCESS:
                            # If the operation has failed, report an error
                            # message
                            logger.warning(
                                'Cannot create SRv6 Explicit Path: %s',
                                response
                            )
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=self.exec_or_mark_device_inconsitent,
                            rollback_func=(
                                self.srv6_manager.remove_srv6_explicit_path
                            ),
                            server_ip=l_deviceip,
                            server_port=self.grpc_client_port,
                            destination=subnet,
                            device=dev,
                            segments=sid_list[:-1],
                            encapmode="encap",
                            table=tableid,
                            deviceid=l_slice['deviceid'],
                            tenantid=tenantid
                        )
                        response = self.srv6_manager.create_iproute(
                            server_ip=l_deviceip,
                            server_port=self.grpc_client_port,
                            destination=sid_list[-2],
                            out_interface=ip6tnl_tx_ifname,
                            table=tableid
                        )
                        if response == SbStatusCode.STATUS_FILE_EXISTS:
                            logger.warning(
                                'Cannot create the route. Route already '
                                'exists. Skipping'
                            )
                        elif response != SbStatusCode.STATUS_SUCCESS:
                            # If the operation has failed, report an error
                            # message
                            logger.warning(
                                'Cannot set route for %s in %s ',
                                subnet,
                                l_deviceip
                            )
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=self.exec_or_mark_device_inconsitent,
                            rollback_func=self.srv6_manager.remove_iproute,
                            server_ip=l_deviceip,
                            server_port=self.grpc_client_port,
                            destination=sid_list[-2],
                            out_interface=ip6tnl_tx_ifname,
                            table=tableid,
                            deviceid=l_slice['deviceid'],
                            tenantid=tenantid
                        )
                    else:
                        # If we are using an IP over IPv6+SRH encapsulation,
                        # we need to redirect the traffic over the SRH tunnel
                        response = self.srv6_manager.create_srv6_explicit_path(
                            l_deviceip,
                            self.grpc_client_port,
                            destination=subnet,
                            table=tableid,
                            device=dev,
                            segments=sid_list,
                            encapmode='encap'
                        )
                        if response == SbStatusCode.STATUS_FILE_EXISTS:
                            logger.warning(
                                'Cannot create the SRv6 path. Path already '
                                'exists. Skipping'
                            )
                        elif response != SbStatusCode.STATUS_SUCCESS:
                            # If the operation has failed, report an error
                            # message
                            logger.warning(
                                'Cannot create SRv6 Explicit Path: %s',
                                response
                            )
                            return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                        # Add reverse action to the rollback stack
                        rollback.push(
                            func=self.exec_or_mark_device_inconsitent,
                            rollback_func=(
                                self.srv6_manager.remove_srv6_explicit_path
                            ),
                            server_ip=l_deviceip,
                            server_port=self.grpc_client_port,
                            destination=subnet,
                            device=dev,
                            segments=sid_list,
                            encapmode="encap",
                            table=tableid,
                            deviceid=l_slice['deviceid'],
                            tenantid=tenantid
                        )
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug('Remote interface assigned to VPN successfully')
        return NbStatusCode.STATUS_OK

    def _create_tunnel_uni_reconciliation_r(
        self,
        overlayid,
        overlay_name,
        overlay_type,
        l_slice,
        r_slice,
        tenantid,
        overlay_info
    ):
        logger.debug(
            'Attempting to create unidirectional tunnel from %s to %s',
            l_slice['interface_name'],
            r_slice['interface_name']
        )
        with RollbackContext() as rollback:
            # Check if the unidirectional tunnel
            # between the two slices already exists
            #
            # Increase the number of tunnels
            # num_tunnels = storage_helper.inc_and_get_tunnels_counter(
            #    overlayid, tenantid, l_slice['deviceid'], r_slice)
            # If the uni tunnel already exists, we have done
            # if num_tunnels > 1:
            #    logger.debug('Skip tunnel %s %s' %
            #                 (l_slice['interface_name'],
            #                  r_slice['interface_name']))
            #    return NbStatusCode.STATUS_OK
            # Configure the tunnel
            #
            # Get router address
            r_deviceip = storage_helper.get_router_mgmtip(
                r_slice['deviceid'], tenantid
            )
            if r_deviceip is None:
                # Cannot get the router address
                logger.warning('Cannot get the router address')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Any non-loopback device
            # We use the WAN interface
            # in order to solve an issue of routes getting deleted when the
            # interface is assigned to a VRF
            dev = storage_helper.get_wan_interfaces(
                l_slice['deviceid'], tenantid
            )
            if dev is None:
                # Cannot get wan interface
                logger.warning('Cannot get WAN interface')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            if len(dev) == 0:
                # Cannot get wan interface
                logger.warning('Cannot get WAN interface. No WAN interfaces')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            dev = dev[0]
            # Get the table ID
            tableid = storage_helper.get_tableid(overlayid, tenantid)
            if tableid is None:
                logger.warning('Cannot retrieve VPN table ID')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get the SID
            sid_list = self.controller_state_srv6.get_sid_list(
                r_slice['deviceid'], tenantid, tableid
            )
            # pyroute2 requires SID list in reverse order
            sid_list = sid_list[::-1]
            # Get the subnets
            # if overlay_type == OverlayType.IPv6Overlay:
            #     subnets = storage_helper.get_ipv6_subnets(
            #         r_slice['deviceid'], tenantid, r_slice['interface_name']
            #     )
            # elif overlay_type == OverlayType.IPv4Overlay:
            #     subnets = storage_helper.get_ipv4_subnets(
            #         r_slice['deviceid'], tenantid, r_slice['interface_name']
            #     )
            # else:
            #     logger.warning('Error: Unsupported VPN type: %s',
            #                    overlay_type)
            #     return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add the tunnel to the controller state and get the tunnel ID of
            # the new tunnel
            tunnel = storage_helper.get_tunnel(
                overlayid=overlayid,
                ldeviceid=l_slice['deviceid'],
                rdeviceid=r_slice['deviceid'],
                tenantid=tenantid
            )
            if tunnel is None:
                logger.warning('Error: Cannot store tunnel')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # If the SID list has only one SID, we can store the SID in the
            # IPv6 destination address field of the packets; in this case, the
            # SRH is not required and the encapsulation becomes an IPv4 over
            # IPv6 encapsulation or IPv6 over IPv6 encapsulation
            if (
                not storage_helper.is_srh_forced(r_slice['deviceid'], tenantid)
                and len(sid_list) == 1
            ):
                # Create a name for the Linux interface and establish the
                # tunnel type
                ip6tnl_ifname = tunnel['tunnel_name']
                # ip6tnl_tx_ifname = '%s-%s-tx' % (ip6tnl_ifname, tableid)
                ip6tnl_rx_ifname = '%s-%s-rx' % (ip6tnl_ifname, tableid)
                if overlay_type == OverlayType.IPv4Overlay:
                    tunnel_type = 'ip4ip6'
                elif overlay_type == OverlayType.IPv6Overlay:
                    tunnel_type = 'ip6ip6'
                else:
                    logger.warning(
                        'Error: Unsupported VPN type: %s', overlay_type
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Create an ip6tnl Linux interface to encapsulate the traffic
                # sent over the tunnel
                # Get a WAN interface
                wan_interfaces = storage_helper.get_wan_interfaces(
                    l_slice['deviceid'], tenantid
                )
                if wan_interfaces is None:
                    # Cannot get WAN interface
                    logger.warning('Cannot get WAN interfaces')
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                if len(wan_interfaces) == 0:
                    # Cannot get WAN interface
                    logger.warning('Cannot get WAN interface')
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                wan_interface = wan_interfaces[0]
                # Get an IPv6 address
                addrs = storage_helper.get_ipv6_addresses(
                    deviceid=l_slice['deviceid'],
                    tenantid=tenantid,
                    interface_name=wan_interface
                )
                if len(addrs) == 0:
                    # No IPv6 address assigned to the interface
                    logging.error(
                        'No IPv6 addresses assigned to the interface %s',
                        wan_interface
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                l_device_wan_ipaddr = addrs[0]
                # Create an ip6tnl Linux interface to decapsulate the traffic
                # received over the tunnel
                # Create the ip6tnl interface
                response = self.srv6_manager.create_ip_tunnel_interface(
                    server_ip=r_deviceip,
                    server_port=self.grpc_client_port,
                    ifname=ip6tnl_rx_ifname,
                    local_addr=sid_list[0],
                    remote_addr=l_device_wan_ipaddr.split('/')[0],
                    tunnel_type=tunnel_type
                )
                if response == SbStatusCode.STATUS_FILE_EXISTS:
                    logger.warning(
                        'Cannot create the tunnel. Tunnel already exists. '
                        'Skipping'
                    )
                elif response != SbStatusCode.STATUS_SUCCESS:
                    logger.warning(
                        'Cannot create the IP Tunnel interface: %s', response
                    )
                    # The operation has failed, return an error message
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=(
                        self.srv6_manager.remove_ip_tunnel_interface
                    ),
                    server_ip=r_deviceip,
                    server_port=self.grpc_client_port,
                    ifname=ip6tnl_rx_ifname,
                    deviceid=r_slice['deviceid'],
                    tenantid=tenantid
                )
                # Add the tunnel interface to the VRF
                response = self.srv6_manager.update_vrf_device(
                    r_deviceip,
                    self.grpc_client_port,
                    name=overlay_name,
                    interfaces=[ip6tnl_rx_ifname],
                    op='add_interfaces'
                )
                if response != SbStatusCode.STATUS_SUCCESS:
                    # If the operation has failed, report an error message
                    logger.warning(
                        'Cannot assign the interface to the VRF: %s', response
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.update_vrf_device,
                    server_ip=r_deviceip,
                    server_port=self.grpc_client_port,
                    name=overlay_name,
                    interfaces=[ip6tnl_rx_ifname],
                    op='del_interfaces',
                    deviceid=r_slice['deviceid'],
                    tenantid=tenantid
                )
                # Get SID prefix length
                public_prefix_length = storage_helper.get_public_prefix_length(
                    r_slice['deviceid'], tenantid
                )
                # Set the IP address
                response = self.srv6_manager.create_ipaddr(
                    r_deviceip,
                    self.grpc_client_port,
                    ip_addr=sid_list[0] + '/' + str(public_prefix_length),
                    device=ip6tnl_rx_ifname,
                    family=AF_INET6
                )
                if response == SbStatusCode.STATUS_FILE_EXISTS:
                    logger.warning(
                        'Cannot assign the IP address. Address already '
                        'exists. Skipping'
                    )
                elif response != SbStatusCode.STATUS_SUCCESS:
                    # If the operation has failed,
                    # report an error message
                    logging.warning(
                        'Cannot assign the IP address to the tunnel interface'
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.remove_ipaddr,
                    server_ip=r_deviceip,
                    server_port=self.grpc_client_port,
                    ip_addr=sid_list[0] + '/' + str(public_prefix_length),
                    device=ip6tnl_rx_ifname,
                    family=AF_INET6,
                    deviceid=r_slice['deviceid'],
                    tenantid=tenantid
                )
            if (
                len(sid_list) > 1
                and (
                    storage_helper.get_outgoing_sr_transparency(
                        l_slice['deviceid'], tenantid
                    ) == 't1'
                    or storage_helper.get_incoming_sr_transparency(
                        r_slice['deviceid'], tenantid
                    ) == 't1'
                )
            ):
                # SID list > 1 and Transparency T1, double encap is required
                # to create the tunnel (IP over IPv6+SRH over IPv6)
                first_tunnel = True  # FIXME
                if first_tunnel:
                    # Create a name for the Linux interface and establish the
                    # tunnel
                    # type
                    ip6tnl_ifname = tunnel['tunnel_name']
                    # ip6tnl_tx_ifname = '%s-%s-tx' % (ip6tnl_ifname, tableid)
                    ip6tnl_rx_ifname = '%s-%s-rx' % (ip6tnl_ifname, tableid)
                    # Create an ip6tnl Linux interface to encapsulate the
                    # traffic sent over the tunnel
                    # Get a WAN interface
                    wan_interfaces = storage_helper.get_wan_interfaces(
                        l_slice['deviceid'], tenantid
                    )
                    if wan_interfaces is None:
                        # Cannot get WAN interface
                        logger.warning('Cannot get WAN interfaces')
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    if len(wan_interfaces) == 0:
                        # Cannot get WAN interface
                        logger.warning('Cannot get WAN interface')
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    wan_interface = wan_interfaces[0]
                    # Get an IPv6 address
                    addrs = storage_helper.get_ipv6_addresses(
                        deviceid=l_slice['deviceid'],
                        tenantid=tenantid,
                        interface_name=wan_interface
                    )
                    if len(addrs) == 0:
                        # No IPv6 address assigned to the interface
                        logging.error(
                            'No IPv6 addresses assigned to the interface %s',
                            wan_interface)
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    l_device_wan_ipaddr = addrs[0]
                    # Create an ip6tnl Linux interface to decapsulate the
                    # traffic received over the tunnel
                    # Create the ip6tnl interface
                    response = self.srv6_manager.create_ip_tunnel_interface(
                        server_ip=r_deviceip,
                        server_port=self.grpc_client_port,
                        ifname=ip6tnl_rx_ifname,
                        local_addr=sid_list[-1],
                        remote_addr=l_device_wan_ipaddr.split('/')[0],
                        tunnel_type='ip6ip6'
                    )
                    if response == SbStatusCode.STATUS_FILE_EXISTS:
                        logger.warning(
                            'Cannot create the tunnel. Tunnel already exists. '
                            'Skipping'
                        )
                    elif response != SbStatusCode.STATUS_SUCCESS:
                        logger.warning(
                            'Cannot create the IP Tunnel interface: %s',
                            response
                        )
                        # The operation has failed, return an error message
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=(
                            self.srv6_manager.remove_ip_tunnel_interface
                        ),
                        server_ip=r_deviceip,
                        server_port=self.grpc_client_port,
                        ifname=ip6tnl_rx_ifname,
                        deviceid=r_slice['deviceid'],
                        tenantid=tenantid
                    )
                    # Add the tunnel interface to the VRF
                    # response = self.srv6_manager.update_vrf_device(
                    #     r_deviceip, self.grpc_client_port, name=overlay_name,
                    #     interfaces=[ip6tnl_rx_ifname],
                    #     op='add_interfaces'
                    # )
                    # if response != SbStatusCode.STATUS_SUCCESS:
                    #     # If the operation has failed, report an error
                    #     # message
                    #     logger.warning(
                    #         'Cannot assign the interface to the VRF: %s'
                    #         % response
                    #     )
                    #     return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Get SID prefix length
                    public_prefix_length = (
                        storage_helper.get_public_prefix_length(
                            r_slice['deviceid'], tenantid
                        )
                    )
                    # Set the IP address
                    response = self.srv6_manager.create_ipaddr(
                        r_deviceip,
                        self.grpc_client_port,
                        ip_addr='2001:db9::2/64',
                        device=ip6tnl_rx_ifname,
                        family=AF_INET6
                    )
                    if response == SbStatusCode.STATUS_FILE_EXISTS:
                        logger.warning(
                            'Cannot assign ip. IP already exists. Skipping'
                        )
                    elif response != SbStatusCode.STATUS_SUCCESS:
                        # If the operation has failed,
                        # report an error message
                        logging.warning(
                            'Cannot assign the IP address to the tunnel '
                            'interface'
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=self.srv6_manager.remove_ipaddr,
                        server_ip=r_deviceip,
                        server_port=self.grpc_client_port,
                        ip_addr='2001:db9::2/64',
                        device=ip6tnl_rx_ifname,
                        family=AF_INET6,
                        deviceid=r_slice['deviceid'],
                        tenantid=tenantid
                    )
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug('Remote interface assigned to VPN successfully')
        return NbStatusCode.STATUS_OK

    def init_overlay_data_reconciliation(
        self,
        overlayid,
        overlay_name,
        tenantid,
        overlay_info
    ):
        logger.debug(
            'Initiating overlay data for the overlay %s', overlay_name
        )
        # Success
        logger.debug(
            'Init overlay data completed for the overlay %s', overlay_name
        )
        return NbStatusCode.STATUS_OK

    def init_tunnel_mode_reconciliation(
        self,
        deviceid,
        tenantid,
        overlay_info
    ):
        logger.debug('Initiating tunnel mode on router %s', deviceid)
        with RollbackContext() as rollback:
            # Initialize the tunnel mode on the router
            #
            # Get the router address
            deviceip = storage_helper.get_device_mgmtip(tenantid, deviceid)
            if deviceip is None:
                # Cannot get the router address
                logger.warning('Cannot get the router address')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # First step: create a rule for local SIDs processing
            # This step is just required for the first VPN
            #
            # Get SID family for this router
            sid_family = self.controller_state_srv6.get_sid_family(
                deviceid, tenantid
            )
            if sid_family is None:
                # If the operation has failed, return an error message
                logger.warning(
                    'Cannot get SID family for deviceid %s', deviceid
                )
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add the rule to steer the SIDs through the local SID table
            # Note: by default there is already an ip rule to steer the packets
            # through the main routing table; therefore, if the local SID
            # table is the main routing table, we don't need to add an ip rule
            # and we can skip this step
            if srv6_controller_utils.LOCAL_SID_TABLE != \
                    srv6_controller_utils.MAIN_ROUTING_TABLE:
                response = self.srv6_manager.create_iprule(
                    deviceip,
                    self.grpc_client_port,
                    family=AF_INET6,
                    table=srv6_controller_utils.LOCAL_SID_TABLE,
                    destination=sid_family
                )
                if response == SbStatusCode.STATUS_FILE_EXISTS:
                    logger.warning(
                        'Cannot create the IP rule. IP rule already exists. '
                        'Skipping'
                    )
                elif response != SbStatusCode.STATUS_SUCCESS:
                    logger.warning(
                        'Cannot create the IP rule for destination %s: %s',
                        sid_family,
                        response
                    )
                    # If the operation has failed, return an error message
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.remove_iprule,
                    server_ip=deviceip,
                    server_port=self.grpc_client_port,
                    family=AF_INET6,
                    table=srv6_controller_utils.LOCAL_SID_TABLE,
                    destination=sid_family,
                    deviceid=deviceid,
                    tenantid=tenantid
                )
            # Add a blackhole route to drop all unknown active segments
            # If the local SID table used to store the segments is the main
            # table, we skip this step
            if srv6_controller_utils.LOCAL_SID_TABLE != \
                    srv6_controller_utils.MAIN_ROUTING_TABLE:
                response = self.srv6_manager.create_iproute(
                    deviceip,
                    self.grpc_client_port,
                    family=AF_INET6,
                    type='blackhole',
                    table=srv6_controller_utils.LOCAL_SID_TABLE
                )
                if response == SbStatusCode.STATUS_FILE_EXISTS:
                    logger.warning(
                        'Cannot create the IP route. Route already exists. '
                        'Skipping'
                    )
                elif response != SbStatusCode.STATUS_SUCCESS:
                    logger.warning(
                        'Cannot create the blackhole route: %s', response
                    )
                    # If the operation has failed, return an error message
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.remove_iproute,
                    server_ip=deviceip,
                    server_port=self.grpc_client_port,
                    family=AF_INET6,
                    type='blackhole',
                    table=srv6_controller_utils.LOCAL_SID_TABLE,
                    deviceid=deviceid,
                    tenantid=tenantid
                )
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug(
            'Init tunnel mode on device %s completed', deviceid
        )
        return NbStatusCode.STATUS_OK

    def init_overlay_reconciliation(
        self,
        overlayid,
        overlay_name,
        overlay_type,
        tenantid,
        deviceid,
        overlay_info
    ):
        logger.debug(
            'Initiating overlay %s on the device %s', overlay_name, deviceid
        )
        with RollbackContext() as rollback:
            # Get the router address
            deviceip = storage_helper.get_device_mgmtip(tenantid, deviceid)
            if deviceip is None:
                # Cannot get the router address
                logger.warning('Cannot get the router address')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Second step is the creation of the decapsulation and lookup route
            if overlay_type == 'IPv6Overlay':
                # For IPv6 VPN we have to perform decap and lookup in IPv6
                # routing table. This behavior is realized by End.DT6 SRv6
                # action
                action = 'End.DT6'
            elif overlay_type == 'IPv4Overlay':
                # For IPv4 VPN we have to perform decap and lookup in IPv6
                # routing table. This behavior is realized by End.DT4 SRv6
                # action
                action = 'End.DT4'
            else:
                logger.warning('Error: Unsupported VPN type: %s', overlay_type)
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get the table ID for the VPN
            logger.debug(
                'Attempting to retrieve the table ID assigned to the VPN'
            )
            tableid = storage_helper.get_tableid(overlayid, tenantid)
            if tableid is None:
                # Table ID not yet assigned
                logger.debug('Cannot get table ID')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            logger.debug('Received table ID:%s', tableid)
            # Third step is the creation of the VRF assigned to the VPN
            response = self.srv6_manager.create_vrf_device(
                deviceip,
                self.grpc_client_port,
                name=overlay_name,
                table=tableid
            )
            if response == SbStatusCode.STATUS_FILE_EXISTS:
                logger.warning(
                   'Cannot create the VRF. VRF already exists. Skipping'
                )
            elif response != SbStatusCode.STATUS_SUCCESS:
                logger.warning(
                    'Cannot create the VRF %s: %s' % (overlay_name, response)
                )
                # If the operation has failed, return an error message
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add reverse action to the rollback stack
            rollback.push(
                func=self.exec_or_mark_device_inconsitent,
                rollback_func=self.srv6_manager.remove_vrf_device,
                server_ip=deviceip,
                server_port=self.grpc_client_port,
                name=overlay_name,
                deviceid=deviceid,
                tenantid=tenantid
            )
            # # Install a blackhole route in the VRF
            # response = self.srv6_manager.create_iproute(
            #     deviceip, self.grpc_client_port, family=AF_INET6,
            #     type='blackhole', table=tableid
            # )
            # if response != SbStatusCode.STATUS_SUCCESS:
            #     logger.warning(
            #         'Cannot create the blackhole route: %s' % response
            #     )
            #     # If the operation has failed, return an error message
            #     return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Get a WAN interface
            # We use the WAN interface
            # in order to solve an issue of routes getting deleted when the
            # interface is assigned to a VRF
            dev = storage_helper.get_wan_interfaces(deviceid, tenantid)
            if dev is None:
                # Cannot get non-loopback interface
                logger.warning('Cannot get non-loopback interface')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            if len(dev) == 0:
                # Cannot get wan interface
                logger.warning(
                    'Cannot get non-loopback interface. No WAN interfaces'
                )
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            dev = dev[0]
            # Get the SID
            logger.debug('Attempting to get a SID for the router')
            sid = self.controller_state_srv6.get_sid(
                deviceid, tenantid, tableid
            )
            logger.debug('Received SID %s' % sid)
            # Add the End.DT4 / End.DT6 route
            response = (
                self.srv6_manager.create_srv6_local_processing_function(
                    deviceip,
                    self.grpc_client_port,
                    segment=sid,
                    action=action,
                    device=dev,
                    localsid_table=srv6_controller_utils.LOCAL_SID_TABLE,
                    table=tableid
                )
            )
            if response == SbStatusCode.STATUS_FILE_EXISTS:
                logger.warning(
                    'Cannot create the seg6local route. Route already exists. '
                    'Skipping'
                )
            elif response != SbStatusCode.STATUS_SUCCESS:
                logger.warning(
                    'Cannot create the SRv6 Local Processing function: %s',
                    response
                )
                # The operation has failed, return an error message
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add reverse action to the rollback stack
            rollback.push(
                func=self.exec_or_mark_device_inconsitent,
                rollback_func=(
                    self.srv6_manager.remove_srv6_local_processing_function
                ),
                server_ip=deviceip,
                server_port=self.grpc_client_port,
                segment=sid,
                localsid_table=srv6_controller_utils.LOCAL_SID_TABLE,
                deviceid=deviceid,
                tenantid=tenantid
            )
            # Enable NDP advertisements for the SID
            if (
                storage_helper.is_proxy_ndp_enabled(deviceid, tenantid)
                and storage_helper.get_public_prefix_length(
                    deviceid, tenantid
                ) != 128
            ):
                # Get the WAN interface
                wan_interfaces = storage_helper.get_wan_interfaces(
                    deviceid, tenantid
                )
                if wan_interfaces is None:
                    # Cannot get wan interface
                    logger.warning('Cannot get WAN interface')
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                if len(wan_interfaces) == 0:
                    # Cannot get wan interface
                    logger.warning(
                        'Cannot get WAN interface. No WAN interfaces'
                    )
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Get the first WAN interface
                dev = wan_interfaces[0]
                # Enable NDP advertisements for the SID
                response = self.srv6_manager.add_proxy_ndp(
                    deviceip,
                    self.grpc_client_port,
                    address=sid,
                    device=dev,
                    family=AF_INET6
                )
                if response == SbStatusCode.STATUS_FILE_EXISTS:
                    logger.warning(
                        'Cannot create the proxy ndp. Proxy already exists. '
                        'Skipping'
                    )
                elif response != SbStatusCode.STATUS_SUCCESS:
                    # If the operation has failed, return an error message
                    logger.warning('Cannot add proxy NDP: %s', response)
                    return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                # Add reverse action to the rollback stack
                rollback.push(
                    func=self.exec_or_mark_device_inconsitent,
                    rollback_func=self.srv6_manager.del_proxy_ndp,
                    server_ip=deviceip,
                    server_port=self.grpc_client_port,
                    address=sid,
                    device=dev,
                    family=AF_INET6,
                    deviceid=deviceid,
                    tenantid=tenantid
                )
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug(
            'Init overlay completed for the overlay %s and the deviceid %s',
            overlay_name,
            deviceid
        )
        return NbStatusCode.STATUS_OK

    def add_slice_to_overlay_reconciliation(
        self,
        overlayid,
        overlay_name,
        deviceid,
        interface_name,
        tenantid,
        overlay_info
    ):
        logger.debug(
            'Attempting to add the slice %s from the router %s '
            'to the overlay %s',
            interface_name,
            deviceid,
            overlay_name
        )
        with RollbackContext() as rollback:
            # Get router address
            deviceip = storage_helper.get_device_mgmtip(tenantid, deviceid)
            if deviceip is None:
                # Cannot get the router address
                logger.warning('Cannot get the router address')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Don't advertise the private customer network
            response = self.srv6_manager.update_interface(
                deviceip,
                self.grpc_client_port,
                name=interface_name,
                ospf_adv=False
            )
            if response == SbStatusCode.STATUS_UNREACHABLE_OSPF6D:
                # If the operation has failed, report an error message
                logger.warning(
                    'Cannot disable OSPF advertisements: '
                    'ospf6d not running'
                )
            elif response != SbStatusCode.STATUS_SUCCESS:
                # If the operation has failed, report an error message
                logger.warning('Cannot disable OSPF advertisements')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add reverse action to the rollback stack
            rollback.push(
                func=self.exec_or_mark_device_inconsitent,
                rollback_func=self.srv6_manager.update_interface,
                server_ip=deviceip,
                server_port=self.grpc_client_port,
                name=interface_name,
                ospf_adv=True,
                deviceid=deviceid,
                tenantid=tenantid
            )
            # Add the interface to the VRF
            response = self.srv6_manager.update_vrf_device(
                deviceip,
                self.grpc_client_port,
                name=overlay_name,
                interfaces=[interface_name],
                op='add_interfaces'
            )
            if response != SbStatusCode.STATUS_SUCCESS:
                # If the operation has failed, report an error message
                logger.warning(
                    'Cannot assign the interface to the VRF: %s', response
                )
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Add reverse action to the rollback stack
            rollback.push(
                func=self.exec_or_mark_device_inconsitent,
                rollback_func=self.srv6_manager.update_vrf_device,
                server_ip=deviceip,
                server_port=self.grpc_client_port,
                name=overlay_name,
                interfaces=[interface_name],
                op='del_interfaces',
                deviceid=deviceid,
                tenantid=tenantid
            )
            # Get the table ID
            tableid = storage_helper.get_tableid(
                overlayid, tenantid)
            if tableid is None:
                logger.warning('Cannot retrieve VPN table ID')
                return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
            # Create the routes for the subnets
            subnets = storage_helper.get_ip_subnets(
                deviceid, tenantid, interface_name)
            for subnet in subnets:
                gateway = subnet['gateway']
                subnet = subnet['subnet']
                if gateway is not None and gateway != '':
                    response = self.srv6_manager.create_iproute(
                        deviceip,
                        self.grpc_client_port,
                        destination=subnet,
                        gateway=gateway,
                        out_interface=interface_name,
                        table=tableid
                    )
                    if response == SbStatusCode.STATUS_FILE_EXISTS:
                        logger.warning(
                            'Cannot create the route. Route already exists. '
                            'Skipping'
                        )
                    elif response != SbStatusCode.STATUS_SUCCESS:
                        # If the operation has failed, report an error message
                        logger.warning(
                            'Cannot set route for %s (gateway %s) in %s ',
                            subnet,
                            gateway,
                            deviceip
                        )
                        return NbStatusCode.STATUS_INTERNAL_SERVER_ERROR
                    # Add reverse action to the rollback stack
                    rollback.push(
                        func=self.exec_or_mark_device_inconsitent,
                        rollback_func=self.srv6_manager.remove_iproute,
                        server_ip=deviceip,
                        server_port=self.grpc_client_port,
                        destination=subnet,
                        gateway=gateway,
                        table=tableid,
                        deviceid=deviceid,
                        tenantid=tenantid
                    )
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug('Add slice to overlay completed')
        return NbStatusCode.STATUS_OK

    def create_tunnel_reconciliation_l(
        self,
        overlayid,
        overlay_name,
        overlay_type,
        l_slice,
        r_slice,
        tenantid,
        overlay_info
    ):
        logger.debug(
            'Attempting to create a tunnel %s between the interfaces %s '
            'and %s',
            overlay_name,
            l_slice['interface_name'],
            r_slice['interface_name']
        )
        with RollbackContext() as rollback:
            # Tunnel from l_slice to r_slice
            res = self._create_tunnel_uni_reconciliation_l(
                overlayid,
                overlay_name,
                overlay_type,
                l_slice,
                r_slice,
                tenantid,
                overlay_info
            )
            if res != NbStatusCode.STATUS_OK:
                return res
            # TODO handle rollback?
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug('Tunnel creation completed')
        return NbStatusCode.STATUS_OK

    def create_tunnel_reconciliation_r(
        self,
        overlayid,
        overlay_name,
        overlay_type,
        l_slice,
        r_slice,
        tenantid,
        overlay_info
    ):
        logger.debug(
            'Attempting to create a tunnel %s between the interfaces %s '
            'and %s',
            overlay_name,
            l_slice['interface_name'],
            r_slice['interface_name']
        )
        with RollbackContext() as rollback:
            # Tunnel from l_slice to r_slice
            res = self._create_tunnel_uni_reconciliation_r(
                overlayid,
                overlay_name,
                overlay_type,
                l_slice,
                r_slice,
                tenantid,
                overlay_info
            )
            if res != NbStatusCode.STATUS_OK:
                return res
            # TODO handle rollback?
            # Success, commit all performed operations
            rollback.commitAll()
        # Success
        logger.debug('Tunnel creation completed')
        return NbStatusCode.STATUS_OK
