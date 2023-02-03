# gRPC Northbound APIs for SRv6 Controller

## Installation ##

## SRv6 Northbound API ##

The project provides a gRPC-based implementations of the SRv6 Northbound API. It includes both the server and the client implementation.

First, set the variable PROTO_FOLDER placed in the file northbound/grpc/nb_grpc_utils.py to point to the folder containing the proto files.

### Run the server ###

    > cd /home/user/repos/srv6-sdn-control-plane/grpc

    Usage: nb_grpc_server.py [options]

    Options:
        -h, --help         Show this help message and exit
        -d, --debug        Activate debug logs
        -s, --secure       Activate secure mode
        -v, --verbose      Enable verbose mode
        -t, --topo-file    [REQUIRED] Filename of the exported topology
        -f, --vpn-file     Filename of the VPN dump
        -c, --certificate  Server certificate file
        -k, --key          Server key file
        -i, --ip           IP address of the gRPC server
        -p, --server-port  Port of the gRPC server
        -o, --client-port  Port of the gRPC clients
        -b, --southbound   Southbound interface chosen from the following list: [grpc]
        -m, --use-mgmt-ip  Use management IPs instead of loopback IPs (for Out-of-Band control)

### Run the client ###

northbound.grpc.**SRv6VPNManager**(secure, certificate)

    SRv6 VPN Manager. Provides methods for creating and managing VPNs.

    Parameters:
        secure       Enable secure mode
        certificate  Server certificate filename

#### Create a VPN ###

northbound.grpc.SRv6Manager.**create_vpn**(server_ip, server_port, vpn_name, vpn_type, interfaces, tenantid)

    Create a new VPN

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        vpn_name     Name of the VPN
        vpn_type     Type of the VPN [IPv4VPN, IPv6VPN]
        interfaces   Interfaces assigned to the VPN
        tenantid     Identifier of the tenant which owns the VPN

#### Remove a VPN ####

northbound.grpc.SRv6Manager.remove_vpn(server_ip, server_port, vpn_name, tenantid)

    Remove a VPN

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        vpn_name     Name of the VPN
        tenantid     Identifier of the tenant which owns the VPN

#### Assign an interface to a VPN ####

northbound.grpc.SRv6Manager.**assign_interface_to_vpn**(server_ip, server_port, vpn_name, tenantid, interface)

    Assign an interface to an existing VPN

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        vpn_name     Name of the VPN
        interface    Interface to be assigned to the VPN
        tenantid     Identifier of the tenant which owns the VPN

#### Remove an interface from a VPN ####

northbound.grpc.SRv6Manager.**remove_interface_from_vpn**(server_ip, server_port, vpn_name, tenantid, interface)

    Remove an interface from an existing VPN

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        vpn_name     Name of the VPN
        intf         Interface to be removed from the VPN
        tenantid     Identifier of the tenant which owns the VPN

#### Get the list of the installed VPNs ####

northbound.grpc.SRv6Manager.**get_vnps**(server_ip, server_port)

    Get the list of the installed VPNs

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server

#### Print the list of the installed VPNs ####

northbound.grpc.SRv6Manager.**print_vpns**(server_ip, server_port)

    Print the list of the installed VPNs

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server

#### Example ####

    # Import the client
    from northbound.grpc import nb_grpc_client
    # Create a SRv6 VPN Manager
    srv6_manager = nb_grpc_client.SRv6VPNManager(secure=False)
    # Create a VPN
    interfaces = [('0.0.0.1', 'ads1-eth3', 'fd00:0:1:1::1', 'fd00:0:1:1::/48'), ('0.0.0.2', 'ads2-eth3', 'fd00:0:1:3::1', 'fd00:0:1:3::/48')]
    srv6_manager.create_vpn('2000::a', 12345, 'research', 'IPv6VPN', interfaces, 10)
    # Print the list of the VPNs
    srv6_manager.print_vpns('2000::a', 12345)
    # Assign an interface to a VPN
    srv6_manager.assign_interface_to_vpn('2000::a', 12345, 'research', 10, ('0.0.0.1', 'ads1-eth4', 'fd00:0:1:2::1', 'fd00:0:1:2::/48'))
    # Remove an interface from a VPN
    srv6_manager.remove_interfacefrom_vpn('2000::a', 12345, 'research', 10, ('0.0.0.1', 'ads1-eth4'))
    # Remove a VPN
    srv6_manager.remove_vpn('2000::a', 12345, 'research', 10)
