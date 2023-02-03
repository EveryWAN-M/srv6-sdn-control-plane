#!/usr/bin/python

from __future__ import absolute_import, division, print_function

# General imports
from six import text_type
import grpc
import json
import sys
import logging
from socket import AF_INET, AF_INET6


# ################## Setup these variables ##################

# Path of the proto files
# PROTO_FOLDER = "../../../srv6-sdn-proto/"

# ###########################################################

# Adjust relative paths
# script_path = os.path.dirname(os.path.abspath(__file__))
# PROTO_FOLDER = os.path.join(script_path, PROTO_FOLDER)

# Check paths
# if PROTO_FOLDER == '':
#    print('Error: Set PROTO_FOLDER variable '
#          'in sb_grpc_client.py')
#    sys.exit(-2)
# if not os.path.exists(PROTO_FOLDER):
#    print('Error: PROTO_FOLDER variable in sb_grpc_client.py '
#          'points to a non existing folder\n')
#    sys.exit(-2)

# Add path of proto files
# sys.path.append(PROTO_FOLDER)

# SRv6 dependencies
from srv6_sdn_control_plane.southbound.grpc import sb_grpc_utils
from srv6_sdn_proto import srv6_manager_pb2
from srv6_sdn_proto import srv6_manager_pb2_grpc
from srv6_sdn_proto.status_codes_pb2 import SbStatusCode
from srv6_sdn_proto import network_events_listener_pb2
from srv6_sdn_proto import network_events_listener_pb2_grpc
from srv6_sdn_proto import empty_req_pb2
from srv6_sdn_proto import gre_interface_pb2
from srv6_sdn_proto import ip_tunnel_interface_pb2


# Network event types
EVENT_TYPES = {
    'CONNECTION_ESTABLISHED': (
        network_events_listener_pb2.NetworkEvent.CONNECTION_ESTABLISHED
    ),
    'INTF_UP': network_events_listener_pb2.NetworkEvent.INTF_UP,
    'INTF_DOWN': network_events_listener_pb2.NetworkEvent.INTF_DOWN,
    'INTF_DEL': network_events_listener_pb2.NetworkEvent.INTF_DEL,
    'NEW_ADDR': network_events_listener_pb2.NetworkEvent.NEW_ADDR,
    'DEL_ADDR': network_events_listener_pb2.NetworkEvent.DEL_ADDR
}

# Define wheter to use SSL or not
DEFAULT_SECURE = False
# SSL cerificate for server validation
DEFAULT_CERTIFICATE = 'cert_client.pem'


# Parser for gRPC errors
def parse_grpc_error(e):
    status_code = e.code()
    details = e.details()
    logging.error(
        'gRPC client reported an error: %s, %s' % (status_code, details)
    )
    if grpc.StatusCode.UNAVAILABLE == status_code:
        code = SbStatusCode.STATUS_GRPC_SERVICE_UNAVAILABLE
    elif grpc.StatusCode.UNAUTHENTICATED == status_code:
        code = SbStatusCode.STATUS_GRPC_UNAUTHORIZED
    else:
        code = SbStatusCode.STATUS_INTERNAL_ERROR
    # Return an error message
    return code


class SRv6Manager:

    def __init__(self, secure=DEFAULT_SECURE, certificate=DEFAULT_CERTIFICATE):
        self.SECURE = secure
        if secure is True:
            if certificate is None:
                print(
                    'Error: "certificate" variable cannot be None in secure '
                    'mode'
                )
                sys.exit(-2)
            self.CERTIFICATE = certificate

    # Build a grpc stub
    def get_grpc_session(self, ip_address, port):
        addr_family = sb_grpc_utils.getAddressFamily(ip_address)
        if addr_family == AF_INET6:
            ip_address = "ipv6:[%s]:%s" % (ip_address, port)
        elif addr_family == AF_INET:
            ip_address = "ipv4:%s:%s" % (ip_address, port)
        else:
            print('Invalid address: %s' % ip_address)
            return
        # If secure we need to establish a channel with the secure endpoint
        if self.SECURE:
            # Open the certificate file
            with open(self.CERTIFICATE, 'rb') as f:
                certificate = f.read()
            # Then create the SSL credentials and establish the channel
            grpc_client_credentials = grpc.ssl_channel_credentials(certificate)
            channel = grpc.secure_channel(
                ip_address, grpc_client_credentials
            )
        else:
            channel = grpc.insecure_channel(ip_address)

        return (srv6_manager_pb2_grpc.SRv6ManagerStub(channel), channel)

    # Shutdown device
    def shutdown_device(self, server_ip, server_port):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Shutdown the device
            response = srv6_stub.ShutdownDevice(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    # CRUD SRv6 Explicit Path

    def create_srv6_explicit_path(self, server_ip, server_port, destination,
                                  device, segments, encapmode="encap",
                                  table=-1):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.SRv6ExplicitPath
        # Create a new SRv6 explicit path request
        path_request = srv6_request.srv6_ep_request
        # Create a new path
        path = path_request.paths.add()
        # Set destination, device, encapmode, table and segments
        path.destination = text_type(destination)
        path.device = text_type(device)
        path.encapmode = text_type(encapmode)
        path.table = int(table)
        for segment in segments:
            # Create a new segment
            srv6_segment = path.sr_path.add()
            srv6_segment.segment = text_type(segment)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Add the SRv6 path
            response = srv6_stub.Create(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def create_srv6_explicit_path_from_json(self, server_ip, server_port,
                                            data):
        json_data = json.loads(data)
        # Iterate over the array and delete one by one all the paths
        for data in json_data:
            # Create message request
            srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
            # Set the type of the carried entity
            srv6_request.entity_type = srv6_manager_pb2.SRv6ExplicitPath
            # Create a new SRv6 explicit path request
            path_request = srv6_request.srv6_ep_request
            # Process JSON file
            for jpath in data['paths']:
                # Create a new path
                path = path_request.paths.add()
                # Set destination, device, encapmode,
                # table and segments
                path.destination = text_type(jpath['destination'])
                path.device = text_type(jpath['device'])
                path.encapmode = text_type(jpath['encapmode'])
                for segment in jpath['segments']:
                    srv6_segment = path.sr_path.add()
                    srv6_segment.segment = text_type(segment)
                try:
                    # Get the reference of the stub
                    srv6_stub, channel = self.get_grpc_session(
                        server_ip, server_port
                    )
                    # Add the SRv6 path
                    response = srv6_stub.Create(srv6_request)
                    # Create the response
                    response = response.status
                except grpc.RpcError as e:
                    # Let's close the session
                    channel.close()
                    # Return the error message
                    return parse_grpc_error(e)
        # Create the response
        return response

    def get_srv6_explicit_path(self, server_ip, server_port, destination,
                               device, segments=[],
                               encapmode="encap", table=-1):
        print('Not yet implemented')
        return SbStatusCode.STATUS_INTERNAL_ERROR

    def update_srv6_explicit_path(self, server_ip, server_port, destination,
                                  device, segments=[],
                                  encapmode="encap", table=-1):
        print('Not yet implemented')
        return SbStatusCode.STATUS_INTERNAL_ERROR

    def remove_srv6_explicit_path(self, server_ip, server_port, destination,
                                  device='', segments=[],
                                  encapmode="encap", table=-1):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.SRv6ExplicitPath
        # Create a new SRv6 explicit path request
        path_request = srv6_request.srv6_ep_request
        # Create a new path
        path = path_request.paths.add()
        # Set destination, device, encapmode, table and segments
        path.destination = text_type(destination)
        path.device = text_type(device)
        path.encapmode = text_type(encapmode)
        path.table = int(table)
        for segment in segments:
            # Create a new segment
            srv6_segment = path.sr_path.add()
            srv6_segment.segment = text_type(segment)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Remove the SRv6 path
            response = srv6_stub.Remove(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def remove_srv6_explicit_path_from_json(self, server_ip, server_port,
                                            data):
        json_data = json.loads(data)
        # Iterate over the array and delete one by one all the paths
        for data in json_data:
            # Create message request
            srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
            # Set the type of the carried entity
            srv6_request.entity_type = srv6_manager_pb2.SRv6ExplicitPath
            # Create a new SRv6 explicit path request
            path_request = srv6_request.srv6_ep_request
            for jpath in data['paths']:
                path = path_request.paths.add()
                # Set destination, device, encapmode
                path.destination = text_type(jpath['destination'])
                path.device = text_type(jpath['device'])
                path.encapmode = text_type(jpath['encapmode'])
                for segment in jpath['segments']:
                    # Create a new segment
                    srv6_segment = path.sr_path.add()
                    srv6_segment.segment = text_type(segment)
                try:
                    # Get the reference of the stub
                    srv6_stub, channel = self.get_grpc_session(
                        server_ip, server_port
                    )
                    # Remove the SRv6 path
                    response = srv6_stub.Remove(srv6_request)
                    # Create the response
                    response = response.status
                except grpc.RpcError as e:
                    # Let's close the session
                    channel.close()
                    # Return the error message
                    return parse_grpc_error(e)
        # Create the response
        return response

    # CRUD SRv6 Local Processing Function

    def create_srv6_local_processing_function(self, server_ip, server_port,
                                              segment, action, device,
                                              localsid_table, nexthop="",
                                              table=-1, interface="",
                                              segments=[]):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.SRv6LocalProcessingFunction
        # Create a new SRv6 Lccal Processing Function request
        function_request = srv6_request.srv6_lpf_request
        # Create a new local processing function
        function = function_request.functions.add()
        # Set segment, action, device, locasid table and other params
        function.segment = text_type(segment)
        function.action = text_type(action)
        function.nexthop = text_type(nexthop)
        function.table = int(table)
        function.interface = text_type(interface)
        function.device = text_type(device)
        function.localsid_table = int(localsid_table)
        for segment in segments:
            # Create a new segment
            srv6_segment = function.segs.add()
            srv6_segment.segment = text_type(segment)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Create the SRv6 local processing function
            response = srv6_stub.Create(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def get_srv6_local_processing_function(self, server_ip, server_port,
                                           segment, action, device,
                                           localsid_table, nexthop="",
                                           table=-1, interface="",
                                           segments=[]):
        print('Not yet implemented')
        return SbStatusCode.STATUS_INTERNAL_ERROR

    def update_srv6_local_processing_function(self, server_ip, server_port,
                                              segment, action, device,
                                              localsid_table, nexthop="",
                                              table=-1, interface="",
                                              segments=[]):
        print('Not yet implemented')
        return SbStatusCode.STATUS_INTERNAL_ERROR

    def remove_srv6_local_processing_function(self, server_ip, server_port,
                                              segment, localsid_table,
                                              action="", nexthop="", table=-1,
                                              interface="", segments=[],
                                              device=""):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.SRv6LocalProcessingFunction
        # Create a new SRv6 Lccal Processing Function request
        function_request = srv6_request.srv6_lpf_request
        # Create a new local processing function
        function = function_request.functions.add()
        # Set segment, action, device, locasid table and other params
        function.segment = text_type(segment)
        function.action = text_type(action)
        function.nexthop = text_type(nexthop)
        function.table = int(table)
        function.interface = text_type(interface)
        function.device = text_type(device)
        function.localsid_table = int(localsid_table)
        for segment in segments:
            # Create a new segment
            srv6_segment = function.segs.add()
            srv6_segment.segment = text_type(segment)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Remove SRv6 local processing function
            response = srv6_stub.Remove(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    # CRUD VRF Device

    def create_vrf_device(self, server_ip, server_port, name, table,
                          interfaces=[]):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.VRFDevice
        # Create a new VRF device request
        vrf_device_request = srv6_request.vrf_device_request
        # Create a new VRF device
        device = vrf_device_request.devices.add()
        # Set name, table
        device.name = text_type(name)
        device.table = int(table)
        # Create a new interfaces
        device.interfaces.extend(interfaces)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Create VRF device
            response = srv6_stub.Create(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def get_vrf_device(self, server_ip, server_port, name, table,
                       interfaces=[]):
        print('Not yet implemented')
        return SbStatusCode.STATUS_INTERNAL_ERROR

    def update_vrf_device(self, server_ip, server_port, name, table=-1,
                          interfaces=[], op=None):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.VRFDevice
        # Create a new VRF device request
        vrf_device_request = srv6_request.vrf_device_request
        # Create a new VRF device
        device = vrf_device_request.devices.add()
        # Set name, table
        device.name = text_type(name)
        if table != -1:
            device.table = int(table)
        # Create a new interfaces
        device.interfaces.extend(interfaces)
        # Operation
        if op not in [
            None,
            'replace_interfaces',
            'add_interfaces',
            'del_interfaces'
        ]:
            print('Invalid operation type: %s' % op)
            return None
        if op is not None:
            device.op = op
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Update VRF device
            response = srv6_stub.Update(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def remove_vrf_device(self, server_ip, server_port, name):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.VRFDevice
        # Create a new VRF device request
        vrf_device_request = srv6_request.vrf_device_request
        # Create a new VRF device
        device = vrf_device_request.devices.add()
        # Set name
        device.name = text_type(name)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Remove the VRF device
            response = srv6_stub.Remove(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    # CRUD Interface

    def create_interface(self, server_ip, server_port, ifindex, name, macaddr,
                         ipaddrs, state, ospf_adv):
        print('Not yet implemented')
        return SbStatusCode.STATUS_INTERNAL_ERROR

    def get_interface(self, server_ip, server_port, interfaces=[]):
        # Get the reference of the stub
        srv6_stub, channel = self.get_grpc_session(
            server_ip, server_port
        )
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.Interface
        # Create a new interface request
        interface_request = srv6_request.interface_request
        # Add interfaces
        for interface in interfaces:
            intf = interface_request.interfaces.add()
            intf.name = text_type(interface)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Create interface
            response = srv6_stub.Update(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def update_interface(self, server_ip, server_port, ifindex=None,
                         name=None, macaddr=None, ipaddrs=None, state=None,
                         ospf_adv=None):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.Interface
        # Create a new interface request
        interface_request = srv6_request.interface_request
        # Create a new interface
        intf = interface_request.interfaces.add()
        # Set name, MAC address and other params
        if ifindex is not None:
            intf.ifindex = int(ifindex)
        if name is not None:
            intf.name = text_type(name)
        if macaddr is not None:
            intf.macaddr = text_type(macaddr)
        if ipaddrs is not None:
            intf.ipaddrs = ipaddrs
        if state is not None:
            intf.state = text_type(state)
        if ospf_adv is not None:
            intf.ospf_adv = bool(ospf_adv)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Create interface
            response = srv6_stub.Update(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def remove_interface(self, server_ip, server_port, ifindex, name, macaddr,
                         ipaddrs, state, ospf_adv):
        print('Not yet implemented')
        return SbStatusCode.STATUS_INTERNAL_ERROR

    # CRUD IP rule

    def create_iprule(self, server_ip, server_port, family, table=-1,
                      priority=-1, action="", scope=-1,
                      destination="", dst_len=-1, source="",
                      src_len=-1, in_interface="", out_interface=""):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPRule
        # Create a new interface request
        rule_request = srv6_request.iprule_request
        # Create a new rule
        rule = rule_request.rules.add()
        # Set family and optional params
        rule.family = int(family)
        rule.table = int(table)
        rule.priority = int(priority)
        rule.action = text_type(action)
        rule.scope = int(scope)
        rule.destination = text_type(destination)
        rule.dst_len = int(dst_len)
        rule.source = text_type(source)
        rule.src_len = int(src_len)
        rule.in_interface = text_type(in_interface)
        rule.out_interface = text_type(out_interface)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Create IP rule
            response = srv6_stub.Create(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def get_iprule(self, server_ip, server_port, family, table=-1,
                   priority=-1, action="", scope=-1,
                   destination="", dst_len=-1, source="",
                   src_len=-1, in_interface="", out_interface=""):
        print('Not yet implemented')
        return SbStatusCode.STATUS_INTERNAL_ERROR

    def update_iprule(self, server_ip, server_port, family, table=-1,
                      priority=-1, action="", scope=-1,
                      destination="", dst_len=-1, source="",
                      src_len=-1, in_interface="", out_interface=""):
        print('Not yet implemented')
        return SbStatusCode.STATUS_INTERNAL_ERROR

    def remove_iprule(self, server_ip, server_port, family, table=-1,
                      priority=-1, action="", scope=-1,
                      destination="", dst_len=-1, source="",
                      src_len=-1, in_interface="", out_interface=""):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPRule
        # Create a new interface request
        rule_request = srv6_request.iprule_request
        # Create a new rule
        rule = rule_request.rules.add()
        # Set family and optional params
        rule.family = int(family)
        rule.table = int(table)
        rule.priority = int(priority)
        rule.action = text_type(action)
        rule.scope = int(scope)
        rule.destination = text_type(destination)
        rule.dst_len = int(dst_len)
        rule.source = text_type(source)
        rule.src_len = int(src_len)
        rule.in_interface = text_type(in_interface)
        rule.out_interface = text_type(out_interface)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Remove IP rule
            response = srv6_stub.Remove(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    # CRUD IP Route

    def create_iproute(self, server_ip, server_port, family=-1, tos="",
                       type="", table=-1, proto=-1, destination="",
                       dst_len=-1, scope=-1, preferred_source="", src_len=-1,
                       in_interface="", out_interface="", gateway=""):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPRoute
        # Create a new interface request
        route_request = srv6_request.iproute_request
        # Create a new route
        route = route_request.routes.add()
        # Set params
        route.family = int(family)
        route.tos = text_type(tos)
        route.type = text_type(type)
        route.table = int(table)
        route.scope = int(scope)
        route.proto = int(proto)
        route.destination = text_type(destination)
        route.dst_len = int(dst_len)
        route.preferred_source = text_type(preferred_source)
        route.src_len = int(src_len)
        route.in_interface = text_type(in_interface)
        route.out_interface = text_type(out_interface)
        route.gateway = text_type(gateway)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Create IP Route
            response = srv6_stub.Create(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def get_iproute(self, server_ip, server_port, family=-1, tos="", type="",
                    table=-1, proto=-1, destination="", dst_len=-1,
                    scope=-1, preferred_source="", src_len=-1,
                    in_interface="", out_interface="", gateway=""):
        print('Not yet implemented')
        return SbStatusCode.STATUS_INTERNAL_ERROR

    def update_iproute(self, server_ip, server_port, family=-1, tos="",
                       type="", table=-1, proto=-1, destination="",
                       dst_len=-1, scope=-1, preferred_source="", src_len=-1,
                       in_interface="", out_interface="", gateway=""):
        print('Not yet implemented')
        return SbStatusCode.STATUS_INTERNAL_ERROR

    def remove_iproute(self, server_ip, server_port, family=-1, tos="",
                       type="", table=-1, proto=-1, destination="",
                       dst_len=-1, scope=-1, preferred_source="", src_len=-1,
                       in_interface="", out_interface="", gateway=""):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPRoute
        # Create a new interface request
        route_request = srv6_request.iproute_request
        # Create a new route
        route = route_request.routes.add()
        # Set params
        route.family = int(family)
        route.tos = text_type(tos)
        route.type = text_type(type)
        route.table = int(table)
        route.proto = int(proto)
        route.scope = int(scope)
        route.destination = text_type(destination)
        route.dst_len = int(dst_len)
        route.preferred_source = text_type(preferred_source)
        route.src_len = int(src_len)
        route.in_interface = text_type(in_interface)
        route.out_interface = text_type(out_interface)
        route.gateway = text_type(gateway)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Remove IP Route
            response = srv6_stub.Remove(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    # CRUD IP Address

    def create_ipaddr(self, server_ip, server_port,
                      ip_addr, device, net='', family=AF_INET,
                      ignore_errors=False):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPAddr
        # Create a new interface request
        addr_request = srv6_request.ipaddr_request
        # Create a new route
        addr = addr_request.addrs.add()
        # Set address, device, family
        addr.ip_addr = text_type(ip_addr)
        addr.device = text_type(device)
        addr.family = int(family)
        addr.net = text_type(net)
        # Flag to ignore errors
        addr_request.ignore_errors = ignore_errors
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Create IP Address
            response = srv6_stub.Create(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def get_ipaddr(self, server_ip, server_port,
                   ip_addr, device, net, family=AF_INET):
        print('Not yet implemented')
        return SbStatusCode.STATUS_INTERNAL_ERROR

    def update_ipaddr(self, server_ip, server_port,
                      ip_addr, device, net, family=AF_INET):
        print('Not yet implemented')
        return SbStatusCode.STATUS_INTERNAL_ERROR

    def remove_ipaddr(self, server_ip, server_port, ip_addr, device, net='',
                      family=-1):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPAddr
        # Create a new interface request
        addr_request = srv6_request.ipaddr_request
        # Create a new route
        addr = addr_request.addrs.add()
        # Set address, device, family
        addr.ip_addr = text_type(ip_addr)
        addr.device = text_type(device)
        addr.family = int(family)
        addr.net = text_type(net)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Remove IP Address
            response = srv6_stub.Remove(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def remove_many_ipaddr(self, server_ip, server_port, addrs,
                           device, nets=None, family=-1):
        if nets is None:
            nets = []
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPAddr
        # Create a new interface request
        if len(nets) == 0:
            for _ in addrs:
                nets.append('')
        elif len(addrs) != len(nets):
            print('Mismatching addrs and nets')
            return None
        addr_request = srv6_request.ipaddr_request
        for (ip_addr, net) in zip(addrs, nets):
            # Create a new route
            addr = addr_request.addrs.add()
            # Set address, device, family
            addr.ip_addr = text_type(ip_addr)
            addr.device = text_type(device)
            addr.family = int(family)
            addr.net = text_type(net)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Remove IP Address
            response = srv6_stub.Remove(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    # CRUD GRE interface

    def create_gre_interface(self, server_ip, server_port, name, local='',
                             remote='', key=-1, type=gre_interface_pb2.GRE):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.GREInterface
        # Create a new interface request
        gre_interface_request = srv6_request.gre_interface_request
        # Create a new route
        gre_interface = gre_interface_request.gre_interfaces.add()
        # Set address, device, family
        gre_interface.name = text_type(name)
        gre_interface.local = text_type(local)
        gre_interface.remote = text_type(remote)
        gre_interface.key = key
        gre_interface.type = type
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Create GRE interface
            response = srv6_stub.Create(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def remove_gre_interface(self, server_ip, server_port, name, local=None,
                             remote=None):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.GREInterface
        # Create a new interface request
        gre_interface_request = srv6_request.gre_interface_request
        # Create a new route
        gre_interface = gre_interface_request.gre_interfaces.add()
        # Set address, device, family
        gre_interface.name = text_type(name)
        gre_interface.local = text_type(local)
        gre_interface.remote = text_type(remote)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Remove GRE interface
            response = srv6_stub.Remove(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    # CRUD IP neigh

    def create_ipneigh(self, server_ip, server_port,
                       dst, lladdr, device, family=AF_INET):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPNeigh
        # Create a new interface request
        neigh_request = srv6_request.ipneigh_request
        # Create a new route
        neigh = neigh_request.neighs.add()
        # Set address, device, family
        neigh.family = int(family)
        neigh.addr = text_type(dst)
        neigh.lladdr = text_type(lladdr)
        neigh.device = text_type(device)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Create IP neigh
            response = srv6_stub.Create(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def remove_ipneigh(self, server_ip, server_port,
                       dst, lladdr, device, family=AF_INET):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPNeigh
        # Create a new interface request
        neigh_request = srv6_request.ipneigh_request
        # Create a new route
        neigh = neigh_request.neighs.add()
        # Set address, device, family
        neigh.family = int(family)
        neigh.addr = text_type(dst)
        neigh.lladdr = text_type(lladdr)
        neigh.device = text_type(device)
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Remove IP neigh
            response = srv6_stub.Create(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def add_proxy_ndp(self, server_ip, server_port,
                      address, family, device):
        """
        Add proxy NDP for an IPv6 address
        """
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPNeigh
        # Create a new IP neighbor request
        neigh_request = srv6_request.ipneigh_request
        # Create a IP neigh entity
        neigh = neigh_request.neighs.add()
        # Set device
        neigh.device = text_type(device)
        # Set family
        neigh.family = int(family)
        # Set IPv6 address
        neigh.addr = text_type(address)
        # Set "proxy" flag
        neigh.proxy = True
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Create IP neigh
            response = srv6_stub.Create(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def del_proxy_ndp(self, server_ip, server_port,
                      address, family, device):
        """
        Remove proxy NDP for an IPv6 address
        """
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPNeigh
        # Create a new IP neighbor request
        neigh_request = srv6_request.ipneigh_request
        # Create a IP neigh entity
        neigh = neigh_request.neighs.add()
        # Set device
        neigh.device = text_type(device)
        # Set family
        neigh.family = int(family)
        # Set IPv6 address
        neigh.addr = text_type(address)
        # Set "proxy" flag
        neigh.proxy = True
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Remove IP neigh
            response = srv6_stub.Remove(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def createVxLAN(self, server_ip, server_port, ifname, vxlan_link, vxlan_id,
                    vxlan_port, vxlan_group=None):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPVxlan
        # Create a new vxlan request
        ipvxlan_request = srv6_request.ipvxlan_request
        # Create a new vxlan
        vxlan = ipvxlan_request.vxlan.add()
        # Set params
        vxlan.ifname = ifname
        vxlan.vxlan_link = vxlan_link
        vxlan.vxlan_id = vxlan_id
        vxlan.vxlan_port = vxlan_port
        if vxlan_group is not None:
            vxlan.vxlan_group = vxlan_group
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Add vxlan
            response = srv6_stub.Create(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def delVxLAN(self, server_ip, server_port, ifname):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPVxlan
        # Create a new VXLAN request
        ipvxlan_request = srv6_request.ipvxlan_request
        # Create VXLAN message
        vxlan = ipvxlan_request.vxlan.add()
        # Set params
        vxlan.ifname = ifname
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Remove VXLAN
            response = srv6_stub.Remove(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def addfdbentries(self, server_ip, server_port, ifindex, dst):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPfdbentries
        # Create a new fdb entries request
        fdbentries_request = srv6_request.fdbentries_request
        # Create a new fdb entries
        fdbentries = fdbentries_request.fdbentries.add()
        # Set params
        fdbentries.ifindex = ifindex
        fdbentries.dst = dst
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Create FDB entries
            response = srv6_stub.Create(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def delfdbentries(self, server_ip, server_port, ifindex, dst):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPfdbentries
        # Create a new FDB entries request
        fdbentries_request = srv6_request.fdbentries_request
        # Create new FDB entry
        fdbentries = fdbentries_request.fdbentries.add()
        # Set params
        fdbentries.ifindex = ifindex
        fdbentries.dst = dst
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Remove fdb entries
            response = srv6_stub.Remove(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def create_ip_tunnel_interface(self, server_ip, server_port, ifname,
                                   local_addr='', remote_addr='',
                                   tunnel_type='ipip'):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPTunnel
        # Create a new ip tunnel request
        iptunnel_request = srv6_request.iptunnel_request
        # Create a new ip tunnel
        iptunnel = iptunnel_request.ip_tunnels.add()
        # Set params
        iptunnel.ifname = ifname
        iptunnel.local_addr = local_addr
        iptunnel.remote_addr = remote_addr
        if tunnel_type == 'ip4ip4':
            iptunnel.tunnel_type = ip_tunnel_interface_pb2.IPTunnelType.IP4IP4
        elif tunnel_type == 'ip4ip6':
            iptunnel.tunnel_type = ip_tunnel_interface_pb2.IPTunnelType.IP4IP6
        elif tunnel_type == 'ip6ip4':
            iptunnel.tunnel_type = ip_tunnel_interface_pb2.IPTunnelType.IP6IP4
        elif tunnel_type == 'ip6ip6':
            iptunnel.tunnel_type = ip_tunnel_interface_pb2.IPTunnelType.IP6IP6
        else:
            logging.error('Invalid tunnel type: %s', tunnel_type)
            return SbStatusCode.STATUS_INTERNAL_ERROR
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Add ip tunnel
            response = srv6_stub.Create(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response

    def remove_ip_tunnel_interface(self, server_ip, server_port, ifname):
        # Create message request
        srv6_request = srv6_manager_pb2.SRv6ManagerRequest()
        # Set the type of the carried entity
        srv6_request.entity_type = srv6_manager_pb2.IPTunnel
        # Create a new ip tunnel request
        iptunnel_request = srv6_request.iptunnel_request
        # Create a new ip tunnel
        iptunnel = iptunnel_request.ip_tunnels.add()
        # Set params
        iptunnel.ifname = ifname
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Remove ip tunnel
            response = srv6_stub.Remove(srv6_request)
            # Create the response
            response = response.status
        except grpc.RpcError as e:
            response = parse_grpc_error(e)
        # Let's close the session
        channel.close()
        # Return the response
        return response


class NetworkEventsListener:

    def __init__(self, secure=DEFAULT_SECURE, certificate=DEFAULT_CERTIFICATE):
        self.SECURE = secure
        if secure is True:
            if certificate is None:
                print(
                    'Error: "certificate" variable cannot be None in secure '
                    'mode'
                )
                sys.exit(-2)
            self.CERTIFICATE = certificate

    # Build a grpc stub
    def get_grpc_session(self, ip_address, port, secure):
        addr_family = sb_grpc_utils.getAddressFamily(ip_address)
        if addr_family == AF_INET6:
            ip_address = "ipv6:[%s]:%s" % (ip_address, port)
        elif addr_family == AF_INET:
            ip_address = "ipv4:%s:%s" % (ip_address, port)
        else:
            print('Invalid address: %s' % ip_address)
            return
        # If secure we need to establish a channel with the secure endpoint
        if secure:
            # Open the certificate file
            with open(self.CERTIFICATE) as f:
                certificate = f.read()
            # Then create the SSL credentials and establish the channel
            grpc_client_credentials = grpc.ssl_channel_credentials(certificate)
            channel = grpc.secure_channel(
                ip_address, grpc_client_credentials
            )
        else:
            channel = grpc.insecure_channel(ip_address)
        return (network_events_listener_pb2_grpc
                .NetworkEventsListenerStub(channel), channel)

    def listen(self, server_ip, server_port):
        # Create message request
        request = empty_req_pb2.EmptyRequest()
        try:
            # Get the reference of the stub
            srv6_stub, channel = self.get_grpc_session(
                server_ip, server_port
            )
            # Listen for Netlink notifications
            for event in srv6_stub.Listen(request):
                # Parse the event
                _event = dict()
                if event.type == EVENT_TYPES['CONNECTION_ESTABLISHED']:
                    # Connection established event
                    _event['type'] = text_type('CONNECTION_ESTABLISHED')
                elif event.type == EVENT_TYPES['INTF_UP']:
                    # Interface UP event
                    _event['interface'] = dict()
                    _event['type'] = 'INTF_UP'
                    # Extract interface index
                    _event['interface']['index'] = int(
                        event.interface.index
                    )
                    # Extract interface name
                    _event['interface']['name'] = text_type(
                        event.interface.name
                    )
                    # Extract interface MAC address
                    _event['interface']['macaddr'] = text_type(
                        event.interface.macaddr
                    )
                elif event.type == EVENT_TYPES['INTF_DOWN']:
                    # Interface DOWN event
                    _event['interface'] = dict()
                    _event['type'] = 'INTF_DOWN'
                    # Extract interface index
                    _event['interface']['index'] = int(
                        event.interface.index
                    )
                    # Extract interface name
                    _event['interface']['name'] = text_type(
                        event.interface.name
                    )
                    # Extract interface MAC address
                    _event['interface']['macaddr'] = text_type(
                        event.interface.macaddr
                    )
                elif event.type == EVENT_TYPES['INTF_DEL']:
                    # Interface DEL event
                    _event['interface'] = dict()
                    _event['type'] = 'INTF_DEL'
                    # Extract interface index
                    _event['interface']['index'] = int(
                        event.interface.index
                    )
                elif event.type == EVENT_TYPES['NEW_ADDR']:
                    # NEW address event
                    _event['interface'] = dict()
                    _event['type'] = 'NEW_ADDR'
                    # Extract interface index
                    _event['interface']['index'] = int(
                        event.interface.index
                    )
                    # Extract address
                    _event['interface']['ipaddr'] = text_type(
                        event.interface.ipaddr
                    )
                elif event.type == EVENT_TYPES['DEL_ADDR']:
                    # DEL address event
                    _event['interface'] = dict()
                    _event['type'] = 'DEL_ADDR'
                    # Extract interface index
                    _event['interface']['index'] = int(
                        event.interface.index
                    )
                    # Extract address
                    _event['interface']['ipaddr'] = text_type(
                        event.interface.ipaddr
                    )
                # Pass the event to the caller
                yield _event
        except grpc.RpcError:  # as e:
            # Let's close the session
            channel.close()
            # Return the response
            # response = parse_grpc_error(e)
            return None
        # Let's close the session
        channel.close()


# Test features
if __name__ == "__main__":
    # Test Netlink messages
    srv6_manager = SRv6Manager()
    # Create a thread for each router and subscribe netlink notifications
    # routers = ["2000::1", "2000::2", "2000::3"]
    # thread_pool = []
    # for router in routers:
    #    thread = Thread(target=srv6_manager
    #                    .createNetlinkNotificationsSubscription,
    #                    args=(router, ))
    #    thread.start()
    #    thread_pool.append(thread)
    # for thread in thread_pool:
    #    thread.join()

    # --- tunnel creation test
    '''srv6_manager.createVxLAN('10.0.14.45', 12345, 'vxlan100','ewED1-eth0', 100, 4789)
    srv6_manager.addfdbentries('10.0.14.45', 12345, 'vxlan100', '10.0.16.49')
    srv6_manager.createVxLAN('10.0.16.49', 12345, 'vxlan100','ewED2-eth0', 100, 4789)
    srv6_manager.addfdbentries('10.0.16.49', 12345, 'vxlan100', '10.0.14.45')
    srv6_manager.create_ipaddr('10.0.14.45',12345, '10.100.0.1/24', 'vxlan100', '')
    srv6_manager.create_ipaddr('10.0.16.49',12345, '10.100.0.2/24', 'vxlan100', '')

    srv6_manager.create_vrf_device('10.0.14.45', 12345, 'vrf1', 1, ['vxlan100', 'ewED1-eth1'])
    srv6_manager.create_vrf_device('10.0.16.49', 12345, 'vrf1', 1, ['vxlan100', 'ewED2-eth1'])

    srv6_manager.create_iproute('10.0.14.45', 12345,  destination='192.168.38.0', dst_len=24, gateway='10.100.0.2', table=1)
    srv6_manager.create_iproute('10.0.16.49', 12345, destination='192.168.32.0', dst_len=24, gateway='10.100.0.1', table=1)'''  # noqa: E501
    # ---- tunnel cancellation test
    '''srv6_manager.delVxLAN('10.0.14.45', 12345, 'vxlan100')
    srv6_manager.remove_vrf_device('10.0.14.45', 12345,  'vrf1')
    srv6_manager.remove_iproute('10.0.16.49', 12345, destination='192.168.32.0', dst_len=24, table=1)
    srv6_manager.delfdbentries('10.0.16.49', 12345, 'vxlan100', '10.0.14.45')'''  # noqa: E501
