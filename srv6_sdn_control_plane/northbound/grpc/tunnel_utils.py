#!/usr/bin/python

from srv6_sdn_control_plane.northbound.grpc import srv6_tunnel
from srv6_sdn_control_plane.northbound.grpc import gre_tunnel
from srv6_sdn_control_plane.northbound.grpc import vxlan_tunnel

tunnel = dict()
tunnel_str = dict()

tunnel_info = dict()


class TunnelState:

    def __init__(self, grpc_client_port, verbose):
        self.tunnel_modes = dict()
        self.init_tunnel_modes(grpc_client_port, verbose)

    def register_tunnel_mode(self, name, tunnel_mode):
        self.tunnel_modes[tunnel_mode.name] = tunnel_mode

    def unregister_tunnel_mode(self, name):
        del self.tunnel_modes[name]

    def init_tunnel_modes(self, grpc_client_port, verbose):
        self.register_tunnel_mode(
            'SRv6',
            srv6_tunnel.SRv6Tunnel(
                grpc_client_port=grpc_client_port, verbose=verbose
            )
        )
        self.register_tunnel_mode(
            'GRE',
            gre_tunnel.GRETunnel(
                grpc_client_port=grpc_client_port, verbose=verbose
            )
        )
        self.register_tunnel_mode(
            'VXLAN',
            vxlan_tunnel.VXLANTunnel(
                grpc_client_port=grpc_client_port, verbose=verbose
            )
        )
