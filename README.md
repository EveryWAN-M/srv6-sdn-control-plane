# SRv6 Controller

This project provides a collection of modules implementing different functionalities of a SDN controller

### Prerequisite ###

This project depends on [SRv6 Properties Generators](https://github.com/netgroup/srv6-properties-generators)

    > cd /home/user/workspace
    > git clone https://github.com/netgroup/srv6-properties-generators
    > cd srv6-properties-generators
    > sudo python setup.py install

This project depends on [SRv6 SDN Proto](https://github.com/netgroup/srv6-sdn-proto)

    > cd /home/user/workspace
    > git clone https://github.com/netgroup/srv6-sdn-proto

### Topology Information Extraction ###

This project includes a module named ***ti_extraction.py*** for extracting network topology from a router running OSPF6 protocol. This module is invoked with a list of ip-port pairs, where ip is the router IP address and port is the port on which OSPF6 daemon VTY is listening, and the interval between two extractions. It buils a NetworkX object and exports it into a JSON file. Optionally, network graph can be also exported as an image file.

Run an example experiment

    > cd /home/user/workspace/srv6-controller

    Usage: ti_extraction.py [options]

    Options:
        -h, --help        Show this help message and exit
        -n, --node-ips    Comma-separated <ip-port> pairs, where ip is the IP address of the router
                          and port is the telnet port of the ospf6d daemon (e.g. 2000::1-2606,2000::2-2606,2000::3-2606)
        -p, --period      Polling period (in seconds)
        -t, --topology    JSON file of the extracted topology
        -g, --topo-graph  Image file of the exported NetworkX graph
        -w, --password    Password of the ospf6d daemon
        -d, --debug       Activate debug logs
        -v, --verbose     Enable verbose mode

You can extract topology from ospf6d

    > ./ti_extraction.py -n 2000::1-2606 -p 1 -t /tmp/topology.json -g /tmp/topology.svg -w srv6

After a while the entity will populate the ***tmp*** folder with the topology files

	> ospf_db (folder containing the dumps extracted from the ospf6d databases)
	> topology.dot (intermediate representation of the topology graph)
	> topology.json (extracted topology)
	> topology.svg (image representing the topology graph)

### SRv6 Southbound API ###

The project provides four different implementations of the SRv6 Southbound API: i) gRPC; ii) NETCONF; iii) REST; iv) SSH.
Each folder contains the client implementation.
The server implementation is contained in the project [srv6-sdn-data-plane](https://github.com/netgroup/srv6-sdn-data-plane).

#### NETCONF, REST, SSH ####

For the client, it is necessary to define the mode and the IP/PORT of the server

    SECURE = False
    srv6_stub,channel = get_grpc_session(IP, PORT, SECURE)

NETCONF and SSH implementation does support only secure mode

Run the client
    
    > cd /home/user/workspace/srv6-controller/*

    Usage: *_client.py

#### gRPC ####

It is necessary to set the variable PROTO_FOLDER placed in the file southbound/grpc/sb_grpc_client.py to point to the folder containing the proto files.

    PROTO_FOLDER = "../../../srv6-sdn-proto/"

southbound.grpc.**SRv6Manager**(secure, certificate)

    SRv6 Manager. Provides methods for handling network capabilities of Linux nodes.

    Parameters:
        secure       Enable secure mode
        certificate  Server certificate filename

Create a SRv6 route

    southbound.grpc.SRv6Manager.**create_srv6_explicit_path**(server_ip, server_port, destination, device, segments, encapmode, table)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        destination  IPv4/IPv6 prefix of the route
        device       Any non-loopback device
        segments     List of segments. Example: ['fc00::1','fc42::5']
        encapmode    'encap' to encapsulate matching packets into an outer IPv6 header containing the SRH, and
                     'inline' to insert the SRH right after the IPv6 header of the original packet.
        table        The table to add this route to

Create a SRv6 routes starting from a str containing a JSON document

    southbound.grpc.SRv6Manager.**create_srv6_explicit_path_from_json**(server_ip, server_port, data)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        data         String containing the JSON document

Remove a SRv6 route

    southbound.grpc.SRv6Manager.**remove_srv6_explicit_path**(server_ip, server_port, destination, device, segments, encapmode, table)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        destination  IPv4/IPv6 prefix of the route
        device       Device of the route
        segments     List of segments. Example: ['fc00::1','fc42::5']
        encapmode    Encap mode of the route ('encap' or 'inline')
        table        The table containing the route

Remove SRv6 routes starting from a str containing a JSON document

    southbound.grpc.SRv6Manager.**remove_srv6_explicit_path**(server_ip, server_port, data)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        data         String containing the JSON document

Remove SRv6 routes starting from a str containing a JSON document

    southbound.grpc.SRv6Manager.**remove_srv6_explicit_path**(server_ip, server_port, data)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        data         String containing the JSON document

Create a SRv6 Local Processing route

    southbound.grpc.SRv6Manager.**create_srv6_local_processing_function**(server_ip, server_port, segment, action, device, localsid_table, nexthop, table, interface, segments)

    Parameters:
        server_ip       IP address of the gRPC server
        server_port     Port of the gRPC server
        segment         Active segment to match, it can be a single address or a prefix
        action          Action to perform (function to apply)
        device          Any non-loopback device
        localsid_table  Local SID table (contains all the segments mapped to a local function)
        nexthop         Parameter used by the End.X, End.DX4 and End.DX6 actions
        table           Parameter used by the End.T, End.DT4 and End.DT6 actions
        interface       Parameter used by the End.DX2 action
        segments        Parameter used by the End.B6 and End.B6.Encaps actions

Remove a SRv6 Local Processing route

    southbound.grpc.SRv6Manager.**remove_srv6_local_processing_function**(server_ip, server_port, segment, localsid_table, action, nexthop, table, interface, segments, device)

    Parameters:
        server_ip       IP address of the gRPC server
        server_port     Port of the gRPC server
        segment         Active segment to match, it can be a single address or a prefix
        action          Action to perform (function to apply)
        device          Any non-loopback device
        localsid_table  Local SID table (contains all the segments mapped to a local function)
        nexthop         Parameter used by the End.X, End.DX4 and End.DX6 actions
        table           Parameter used by the End.T, End.DT4 and End.DT6 actions
        interface       Parameter used by the End.DX2 action
        segments        Parameter used by the End.B6 and End.B6.Encaps actions

Create a VRF device

    southbound.grpc.SRv6Manager.**create_vrf_device**(server_ip, server_port, name, table, interfaces)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        name         Name of the VRF device to be created
        table        FIB table associated to the VRF device
        interfaces   List of the interfaces assigned to the VRF device

Update a VRF device

    southbound.grpc.SRv6Manager.**update_vrf_device**(server_ip, server_port, name, table, interfaces)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        name         Name of the VRF device to be updated
        table        FIB table associated to the VRF device
        interfaces   List of the interfaces assigned to the VRF device

Remove a VRF device

    southbound.grpc.SRv6Manager.**remove_vrf_device**(server_ip, server_port, name)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        name         Name of the VRF device to be removed

Get an interface

    southbound.grpc.SRv6Manager.**get_interface**(server_ip, server_port, interfaces)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        interfaces   List of interface names

Update an interface

    southbound.grpc.SRv6Manager.**update_interface**(server_ip, server_port, ifindex, name, macaddr, ipaddrs, state, ospf_adv)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        ifindex      Index of the interface [unused]
        name         Name of the interface
        macaddr      MAC address of the interface [unused]
        ipaddrs      IP addresses associated to the interface [unused]
        state        State of the interface (e.g. 'UP' or 'DOWN') [unused]
        ospf_adv     True if the OSPF advertise this interface, False otherwise

Create an IP rule

    southbound.grpc.SRv6Manager.**create_iprule**(server_ip, server_port, family, table, priority, action, scope, destination, dst_len, source, src_len, in_interface, out_interface)

    Parameters:
        server_ip      IP address of the gRPC server
        server_port    Port of the gRPC server
        family         Family of the rule (AF_INET or AF_INET6)
        table          The routing table identifier to lookup if the rule selector matches
        priority       The priority of the rule
        action         The action to perform if the selector matches the packet
        scope          The routing scope ('RT_SCOPE_UNIVERSE', 'RT_SCOPE_SITE',  'RT_SCOPE_LINK', 'RT_SCOPE_HOST', 'RT_SCOPE_NOWHERE')
        destination    The destination prefix to match
        dst_len        The prefix length of the destination
        source         The source prefix to match
        src_len        The prefix length of the source
        in_interface   The incoming device to match
        out_interface  The outgoing device to match

Remove an IP rule

    southbound.grpc.SRv6Manager.**remove_iprule**(server_ip, server_port, family, table, priority, action, scope, destination, dst_len, source, src_len, in_interface, out_interface)

    Parameters:
        server_ip      IP address of the gRPC server
        server_port    Port of the gRPC server
        family         Family of the rule (AF_INET or AF_INET6)
        table          The routing table identifier to lookup if the rule selector matches
        priority       The priority of the rule
        action         The action to perform if the selector matches the packet
        scope          The routing scope ('RT_SCOPE_UNIVERSE', 'RT_SCOPE_SITE',  'RT_SCOPE_LINK', 'RT_SCOPE_HOST', 'RT_SCOPE_NOWHERE')
        destination    The destination prefix to match
        dst_len        The prefix length of the destination
        source         The source prefix to match
        src_len        The prefix length of the source
        in_interface   The incoming device to match
        out_interface  The outgoing device to match

Create an IP route

    southbound.grpc.SRv6Manager.**create_iproute**(server_ip, server_port, family, tos, type, table, proto, destination, dst_len, scope, preferred_source, src_len, in_interface, out_interface, gateway)

    Parameters:
        server_ip         IP address of the gRPC server
        server_port       Port of the gRPC server
        family            Family of the destination address
        tos               Type Of Service
        type              The type of the route
        table             The table to add this route to
        proto             The routing protocol identifier of this route
        destination       The destination prefix of the route
        dst_len           The prefix length of the destination address
        scope             The scope of the destinations covered by the route prefix
        preferred_source  The source address to prefer when sending to the destinations covered by the route prefix
        src_len           The prefix length of the source address
        in_interface      The device from which this packet is expected to arrive
        out_interface     Force the output device on which this packet will be routed
        gateway           The address of the nexthop router

Remove an IP route

    southbound.grpc.SRv6Manager.**remove_iproute**(server_ip, server_port, family, tos, type, table, proto, destination, dst_len, scope, preferred_source, src_len, in_interface, out_interface, gateway)

    Parameters:
        server_ip         IP address of the gRPC server
        server_port       Port of the gRPC server
        family            Family of the destination address
        tos               Type Of Service
        type              The type of the route
        table             The table to add this route to
        proto             The routing protocol identifier of this route
        destination       The destination prefix of the route
        dst_len           The prefix length of the destination address
        scope             The scope of the destinations covered by the route prefix
        preferred_source  The source address to prefer when sending to the destinations covered by the route prefix
        src_len           The prefix length of the source address
        in_interface      The device from which this packet is expected to arrive
        out_interface     Force the output device on which this packet will be routed
        gateway           The address of the nexthop router

Assign a IP address to a device

    southbound.grpc.SRv6Manager.**create_ipaddr**(server_ip, server_port, ip_addr, device, net, family=AF_INET)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        ip_addr      IP address to assign to the device
        device       Name of the device
        net          Network prefix to assign to the device
        family       Family of the address (AF_INET or AF_INET6)

Remove a IP address from a device

    southbound.grpc.SRv6Manager.**create_ipaddr**(server_ip, server_port, addrs, nets, device, family=-1)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        addrs        IP addresses to remove from the device
        device       Name of the device
        nets         Network prefixes to remove from the device
        family       Family of the addresses (AF_INET or AF_INET6). If it is set to -1, remove both AF_INET and AF_INET6 addresses

Remove a list of IP addresses from a device

    southbound.grpc.SRv6Manager.**create_ipaddr**(server_ip, server_port, ip_addr, net, device, family=-1)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        ip_addr      IP address to remove from the device
        device       Name of the device
        net          Network prefixes to remove from the device
        family       Family of the address (AF_INET or AF_INET6). If it is set to -1, remove both AF_INET and AF_INET6 addresses




southbound.grpc.**NetworkEventsListener**(secure, certificate)

    Network Events Listener. Provides methods for subscribing network events from the nodes.

    Parameters:
        secure       Enable secure mode
        certificate  Server certificate filename

Listen for network events from a node

    southbound.grpc.NetworkEventsListener.**listen**(server_ip, server_port)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server

### SRv6 Northbound API ###

The project provides a gRPC-based implementations of the SRv6 Northbound API. It includes both the server and the client implementation.

First, set the variable PROTO_FOLDER placed in the file northbound/grpc/nb_grpc_utils.py to point to the folder containing the proto files.

#### Run the server ####

    > cd /home/user/workspace/srv6-sdn-control-plane/grpc

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

#### Run the client ####

northbound.grpc.**SRv6VPNManager**(secure, certificate)

    SRv6 VPN Manager. Provides methods for creating and managing VPNs.

    Parameters:
        secure       Enable secure mode
        certificate  Server certificate filename

Create a new VPN

    northbound.grpc.SRv6Manager.**create_vpn**(server_ip, server_port, vpn_name, vpn_type, interfaces, tenantid)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        vpn_name     Name of the VPN
        vpn_type     Type of the VPN [IPv4VPN, IPv6VPN]
        interfaces   Interfaces assigned to the VPN
        tenantid     Identifier of the tenant which owns the VPN

Remove a VPN
    
    northbound.grpc.SRv6Manager.**remove_vpn**(server_ip, server_port, vpn_name, tenantid)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        vpn_name     Name of the VPN
        tenantid     Identifier of the tenant which owns the VPN

Assign an interface to an existing VPN

    northbound.grpc.SRv6Manager.**assign_interface_to_vpn**(server_ip, server_port, vpn_name, tenantid, interface)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        vpn_name     Name of the VPN
        interface    Interface to be assigned to the VPN
        tenantid     Identifier of the tenant which owns the VPN

Remove an interface from an existing VPN

    northbound.grpc.SRv6Manager.**remove_interface_from_vpn**(server_ip, server_port, vpn_name, tenantid, interface)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server
        vpn_name     Name of the VPN
        intf         Interface to be removed from the VPN
        tenantid     Identifier of the tenant which owns the VPN

Get the list of the installed VPNs

    northbound.grpc.SRv6Manager.**get_vnps**(server_ip, server_port)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server

Print the list of the installed VPNs

    northbound.grpc.SRv6Manager.**print_vpns**(server_ip, server_port)

    Parameters:
        server_ip    IP address of the gRPC server
        server_port  Port of the gRPC server

Example

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

### Interface discovery ###

The project includes a module named ***interface_discovery.py*** for extracting the interfaces information from a router by using the Southbound API. This module is invoked with a list of ip-port pairs, where ip is the router IP address and port is the port on which gRPC server is listening. It builds a dict containing information about the interfaces and exports it into a JSON file.

    -n, --node-ips      Comma-separated <ip-port> pairs, where ip is the IP address of the router and port is 
                        the port of the gRPC server (e.g. 2000::1-12345,2000::2-12345,2000::3-12345)
    -i, --interfaces    JSON file of the extracted interfaces
    -s, --sb-interface  Southbound interface used to interact with the nodes, chosen from this list [grpc]
    -v, --verbose       Enable verbose mode

### SRv6 Controller ###

The project includes a module named ***srv6_controller.py*** for automate the task of configuring and starting all the controller modules.

    --ips                 IP of the routers from which the topology has to be extracted, comma-separated IP-PORT maps
                          (i.e. 2000::1-2606,2000::2-2606,2000::3-2606)
    -p, --period          Topology information extraction period
    --In-Band             Enable In-Band SRv6 Controller
    --Topology            File where the topology extracted has to be saved
    --topo-graph          File where the topology graph image has to be saved
    -d, --debug           Activate debug logs
    --Password            Password used to log in to ospf6d daemon
    -v, --verbose         Enable verbose mode
    --sb-interface        Select a southbound interface from this list: %s' % SUPPORTED_SB_INTERFACES
    --nb-interface        Select a northbound interface from this list: %s' % SUPPORTED_NB_INTERFACES
    --grpc-server-ip      IP of the northbound gRPC server
    --grpc-server-port    Port of the northbound gRPC server
    --grpc-client-port    help='Port of the northbound gRPC client
    -s, --secure          Activate secure mode
    --server-cert         Server certificate file
    --server-key          Server key file
    -f, --vpn-file        File where the vpns created have to be saved
    --min-interval-dumps  Minimum interval between two consecutive dumps
