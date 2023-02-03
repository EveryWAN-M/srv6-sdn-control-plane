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

from __future__ import absolute_import, division, print_function

# General imports
import logging
from socket import AF_UNSPEC
from socket import AF_INET
from socket import AF_INET6
# ipaddress dependencies
from ipaddress import IPv6Interface, IPv4Interface
# SRv6 dependencies
from srv6_sdn_control_plane.northbound.grpc import tunnel_mode
from srv6_sdn_control_plane.northbound.grpc import gre_tunnel_utils
from srv6_sdn_control_plane.southbound.grpc import sb_grpc_client
from srv6_sdn_control_plane import srv6_controller_utils
from srv6_sdn_control_plane.srv6_controller_utils import OverlayType
from srv6_sdn_proto import status_codes_pb2
from srv6_sdn_proto import gre_interface_pb2


# Global variables definition

# Default gRPC client port
DEFAULT_GRPC_CLIENT_PORT = 12345
# Verbose mode
DEFAULT_VERBOSE = False
# Logger reference
logger = logging.getLogger(__name__)


class GRETunnel(tunnel_mode.TunnelMode):
    """gRPC request handler"""

    def __init__(self, grpc_client_port=DEFAULT_GRPC_CLIENT_PORT,
                 controller_state=None, verbose=DEFAULT_VERBOSE):
        # Name of the tunnel mode
        self.name = 'GRE'
        # Port of the gRPC client
        self.grpc_client_port = grpc_client_port
        # Verbose mode
        self.verbose = verbose
        # Create SRv6 Manager
        self.srv6_manager = sb_grpc_client.SRv6Manager()
        # Initialize controller state
        self.controller_state = controller_state
        # Initialize controller state
        self.controller_state_gre = gre_tunnel_utils.ControllerStateGRE(
            controller_state
        )

    def _add_site_to_overlay(self, overlay_name, tenantid, local_site,
                             remote_site, overlay_info):
        # Get the VPN type
        overlay_type = self.controller_state_gre.overlay_type[overlay_name]

        local_router = self.controller_state.get_router_mgmtip(
            local_site.routerid, tenantid
        )
        remote_router = self.controller_state.get_router_mgmtip(
            remote_site.routerid, tenantid
        )

        tableid = self.controller_state_gre.get_tableid(overlay_name)

        vrf_interfaces = self.controller_state_gre.vrf_interfaces

        vrf_name = 'vrf-%s' % overlay_name
        if local_router not in vrf_interfaces:
            vrf_interfaces[local_router] = dict()
        if vrf_name not in vrf_interfaces[local_router]:
            self.srv6_manager.create_vrf_device(
                local_router,
                self.grpc_client_port,
                name=vrf_name,
                table=tableid
            )
            vrf_interfaces[local_router][vrf_name] = set(
            )

        gre_key = self.controller_state_gre.get_gre_key(
            overlay_name, local_router, remote_router
        )
        if gre_key == -1:
            gre_key = self.controller_state_gre.get_new_gre_key(
                overlay_name, tenantid, local_router, remote_router
            )

        if overlay_type == OverlayType.IPv4Overlay:
            type = gre_interface_pb2.GRE
            family = AF_INET
        elif overlay_type == OverlayType.IPv6Overlay:
            type = gre_interface_pb2.IP6GRE
            family = AF_INET6
        else:
            logger.error('Type not supported')
            return

        routerid = remote_site.routerid
        gre_name = 'gre-%s-%s-%s' % (overlay_name, routerid[:3], gre_key)
        if gre_name not in vrf_interfaces[local_router][vrf_name]:
            self.srv6_manager.create_gre_interface(
                local_router,
                self.grpc_client_port,
                name=gre_name,
                local=local_router,
                remote=remote_router,
                key=gre_key,
                type=type
            )
            self.srv6_manager.update_vrf_device(
                local_router,
                self.grpc_client_port,
                name=vrf_name,
                interfaces=[gre_name]
            )
            vrf_interfaces[local_router][vrf_name].add(
                gre_name
            )
        if local_site.interface_name not in vrf_interfaces[
            local_router
        ][vrf_name]:
            vrf_interfaces[local_router][vrf_name].add(
                local_site.interface_name
            )
            interfaces = vrf_interfaces[local_router][vrf_name]
            self.srv6_manager.update_vrf_device(
                local_router,
                self.grpc_client_port,
                name=vrf_name,
                interfaces=interfaces
            )
            # Remove all IPv4 and IPv6 addresses
            ipv4_addrs = self.controller_state.get_interface_ipv4(
                local_site.routerid, local_site.interface_name
            )
            if ipv4_addrs is None:
                # If the operation has failed, return an error message
                logger.warning('Cannot get interface addresses')
                return status_codes_pb2.STATUS_INTERNAL_ERROR
            ipv6_addrs = self.controller_state.get_interface_ipv6(
                local_site.routerid, local_site.interface_name
            )
            if ipv6_addrs is None:
                # If the operation has failed, return an error message
                logger.warning('Cannot get interface addresses')
                return status_codes_pb2.STATUS_INTERNAL_ERROR
            addrs = list()
            nets = list()
            for addr in ipv4_addrs:
                addrs.append(addr)
                nets.append(str(IPv4Interface(addr).network))
            for addr in ipv6_addrs:
                addrs.append(addr)
                nets.append(str(IPv6Interface(addr).network))

            response = self.srv6_manager.remove_many_ipaddr(
                local_router,
                self.grpc_client_port,
                addrs=addrs,
                nets=nets,
                device=local_site.interface_name,
                family=AF_UNSPEC
            )
            if response != status_codes_pb2.STATUS_SUCCESS:
                # If the operation has failed, report an error message
                logger.warning(
                    'Cannot remove the public addresses from the interface'
                )
                return status_codes_pb2.STATUS_INTERNAL_ERROR
            # Don't advertise the private customer network
            response = self.srv6_manager.update_interface(
                local_router,
                self.grpc_client_port,
                name=local_site.interface_name,
                ospf_adv=False
            )
            if response == status_codes_pb2.STATUS_UNREACHABLE_OSPF6D:
                # If the operation has failed, report an error message
                logger.warning(
                    'Cannot disable OSPF advertisements: ospf6d not running'
                )
            elif response != status_codes_pb2.STATUS_SUCCESS:
                # If the operation has failed, report an error message
                logger.warning('Cannot disable OSPF advertisements')
                return status_codes_pb2.STATUS_INTERNAL_ERROR
            # Add IP address to the interface
            if srv6_controller_utils.getAddressFamily(
                local_site.interface_ip
            ) == AF_INET:
                net = IPv4Interface(local_site.interface_ip).network.__str__()
            elif srv6_controller_utils.getAddressFamily(
                local_site.interface_ip
            ) == AF_INET6:
                net = IPv6Interface(local_site.interface_ip).network.__str__()
            else:
                logging.warning('Invalid IP address')

            response = self.srv6_manager.create_ipaddr(
                local_router,
                self.grpc_client_port,
                ip_addr=local_site.interface_ip,
                device=local_site.interface_name,
                net=net,
                family=family
            )
            if response != status_codes_pb2.STATUS_SUCCESS:
                # If the operation has failed, report an error message
                logger.warning(
                    'Cannot assign the private VPN IP address to the interface'
                )
                return status_codes_pb2.STATUS_INTERNAL_ERROR

        for subnet in remote_site.subnets:
            self.srv6_manager.create_iproute(
                local_router,
                self.grpc_client_port,
                destination=subnet,
                out_interface=gre_name,
                table=tableid
            )

    def create_overlay_net(self, overlay_name, overlay_type, sites, tenantid,
                           overlay_info):
        self.controller_state_gre.get_new_tableid(overlay_name, tenantid)

        self.controller_state_gre.overlay_type[overlay_name] = overlay_type

        self.controller_state_gre.interfaces_in_vpn[overlay_name] = set()

        for site in sites:
            self.add_site_to_overlay(
                overlay_name, tenantid, site, overlay_info
            )

    def remove_overlay_net(self, overlay_name, tenantid, overlay_info):
        raise NotImplementedError

    def add_site_to_overlay(self, overlay_name, tenantid, site, overlay_info):
        tunneled_interfaces = self.controller_state_gre.interfaces_in_vpn[
            overlay_name
        ]
        for site2 in tunneled_interfaces:
            self._add_site_to_overlay(
                overlay_name, tenantid, site, site2, overlay_info
            )
            self._add_site_to_overlay(
                overlay_name, tenantid, site2, site, overlay_info
            )

        self.controller_state_gre.interfaces_in_vpn[overlay_name].add(site)

    def remove_site_from_overlay(self, overlay_name, tenantid, site,
                                 overlay_info):
        raise NotImplementedError

    def get_overlays(self):
        raise NotImplementedError
