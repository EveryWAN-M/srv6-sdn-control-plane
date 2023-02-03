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
# Topology information extraction
#
# @author Carmine Scarpitta <carmine.scarpitta.94@gmail.com>
# @author Pier Luigi Ventre <pier.luigi.ventre@uniroma2.it>
# @author Stefano Salsano <stefano.salsano@uniroma2.it>
#

from __future__ import absolute_import, division, print_function

# General imports
from six import text_type
import json
import os
import time
import telnetlib
import re
import socket
import errno
import logging
from argparse import ArgumentParser
# NetworkX dependencies
import networkx as nx
from networkx.drawing.nx_agraph import write_dot
from networkx.readwrite import json_graph
# ipaddress dependencies
from ipaddress import IPv6Network
from ipaddress import IPv4Address
from ipaddress import IPv6Address

# ################## Setup these variables ##################

# Loopback prefix used by the routers
LOOPBACK_PREFIX = 'fcff::'

# ###########################################################


# Default topology file
DEFAULT_TOPOLOGY_FILE = '/tmp/topology.json'
# Interval between two consecutive extractions (in seconds)
DEFAULT_TOPO_EXTRACTION_PERIOD = 10
# In our experiment we use srv6 as default password
DEFAULT_OSPF6D_PASSWORD = 'srv6'
# Dot file used to draw topology graph
DOT_FILE_TOPO_GRAPH = '/tmp/topology.dot'
# Default folder where to save extracted OSPF databases
OSPF_DB_PATH = '/tmp/ospf_db'
# Convert str to unicode
LOOPBACK_PREFIX = text_type(LOOPBACK_PREFIX)


def print_and_die(message, code=-2):
    print(message)
    exit(code)


def connect_telnet(router, port):
    # Establish a telnet connection to the router
    try:
        # Init telnet
        tn = telnetlib.Telnet(router, port, 3)
        # Connection established
        return tn
    except socket.timeout:
        # Timeout expired
        logging.error('Error: cannot establish a connection '
                      'to %s on port %s\n' % (str(router), str(port)))
    except socket.error as e:
        # Socket error
        if e.errno != errno.EINTR:
            logging.error('Error: cannot establish a connection '
                          'to %s on port %s\n' % (str(router), str(port)))
    return None


# Build NetworkX Topology graph
def build_topo_graph(routers, stub_networks, transit_networks):
    # Topology graph
    G = nx.Graph()
    # Add routers to the graph
    for routerid, router_info in routers.items():
        # Extract loopback net and loopback ip address
        loopbacknet = router_info['loopbacknet']
        loopbackip = router_info['loopbackip']
        # Add the node to the graph
        G.add_node(routerid, routerid=routerid, fillcolor='red',
                   style='filled', shape='ellipse',
                   loopbacknet=loopbacknet, loopbackip=loopbackip,
                   type='router')
    # Add stub networks to the graph
    for net in stub_networks.keys():
        G.add_node(net, fillcolor='cyan', style='filled',
                   shape='box', type='stub_network')
    # Build edges list
    # Process transit networks
    for net, _routerids in transit_networks.items():
        # Link between two routers
        edge = list(_routerids)
        edge = (edge[0], edge[1])
        # Add edge to the graph
        # This is a transit network, set the net as label
        G.add_edge(*edge, label=net, fontsize=9, net=net)
    # Process edge networks
    for net, routerids in stub_networks.items():
        # Link between a router and a stub network
        edge = (list(routerids)[0], net)
        # Add edge to the graph
        # This is a stub network, no label on the edge
        G.add_edge(*edge, label='', fontsize=9, net=net)
    return G


def connect_and_extract_topology(ips_ports, ospfdb_path=OSPF_DB_PATH,
                                 ospf6d_pwd=DEFAULT_OSPF6D_PASSWORD,
                                 verbose=False):
    # Check if ospfdb_path exists, if not create it
    if not os.path.exists(ospfdb_path):
        os.makedirs(ospfdb_path)
    # General data structures
    # Routers info
    routers_dict = dict()
    # Stub networks dictionary: mapping stub networks to sets
    # of routers advertising the networks
    stub_networks = dict()
    # Transit networks dictionary: mapping transit networks
    # to sets of routers advertising the networks
    transit_networks = dict()
    # Mapping network id to network ipv6 prefix
    netid_to_netprefix = dict()
    # Mapping router id to router
    routerid_to_router = dict()
    # Mapping router id to loopback net
    routerid_to_loopbacknet = dict()
    # Mapping router id to loopback IP
    routerid_to_loopbackip = dict()
    # Let's parse the input
    routers = []
    ports = []
    # First create the chunk
    for ip_port in ips_ports:
        # Then parse the chunk
        data = ip_port.split("-")
        routers.append(data[0])
        ports.append(data[1])
    # Iterate on routers
    for router, port in zip(routers, ports):
        if verbose:
            print('\n********* Connecting to %s-%s *********' % (router, port))
        # Password of ospf6d daemon
        password = ospf6d_pwd
        # Establish a telnet connection
        # to the ospf6d daemon executing on the router
        ospf6d_conn = connect_telnet(router, port)
        # If the router is unreachable, try to connect to the next router
        if ospf6d_conn is None:
            continue
        # Insert login password
        if password:
            ospf6d_conn.read_until(b'Password: ')
            ospf6d_conn.write(password.encode('latin-1') + b'\r\n')
        # Terminal length set to 0 to not have interruptions
        ospf6d_conn.write(b'terminal length 0\r\n')
        # Get routing details from ospf6 database
        ospf6d_conn.write(b'show ipv6 ospf6 route intra-area detail\r\n')
        # Close
        ospf6d_conn.write(b'q\r\n')
        # Get results
        route_details = ospf6d_conn.read_all().decode()
        # Establish a telnet connection
        # to the ospf6d daemon executing on the router
        ospf6d_conn = connect_telnet(router, port)
        # If the router is unreachable, try to connect to the next router
        if ospf6d_conn is None:
            continue
        # Insert login password
        if password:
            ospf6d_conn.read_until(b'Password: ')
            ospf6d_conn.write(password.encode('latin-1') + b'\r\n')
        # Terminal length set to 0 to not have interruptions
        ospf6d_conn.write(b'terminal length 0\r\n')
        # Get network details from ospf6 database
        ospf6d_conn.write(b'show ipv6 ospf6 database network detail\r\n')
        # Close
        ospf6d_conn.write(b'q\r\n')
        # Get results
        network_details = ospf6d_conn.read_all().decode()
        # Establish a telnet connection
        # to the ospf6d daemon executing on the router
        ospf6d_conn = connect_telnet(router, port)
        # If the router is unreachable, try to connect to the next router
        if ospf6d_conn is None:
            continue
        # Insert login password
        if password:
            ospf6d_conn.read_until(b'Password: ')
            ospf6d_conn.write(password.encode('latin-1') + b'\r\n')
        # Terminal length set to 0 to not have interruptions
        ospf6d_conn.write(b'terminal length 0\r\n')
        # Turn on privileged mode
        ospf6d_conn.write(b'enable\r\n')
        # Configuration terminal
        ospf6d_conn.write(b'configure terminal\r\n')
        # Get running configuration
        ospf6d_conn.write(b'show running-config\r\n')
        # Close
        ospf6d_conn.write(b'q\r\n')
        # Close
        ospf6d_conn.write(b'q\r\n')
        # Get results
        running_config = ospf6d_conn.read_all().decode()
        # Close telnet connection
        ospf6d_conn.close()
        # Write route database to a file for post-processing
        with open('%s/route-detail-%s-%s.txt'
                  % (ospfdb_path, router, port), 'w') as route_file:
            route_file.write(route_details)
        # Write network database to a file for post-processing
        with open('%s/network-detail-%s-%s.txt'
                  % (ospfdb_path, router, port), 'w') as network_file:
            network_file.write(network_details)
        # Write running config to a file for post-processing
        with open('%s/running-config-%s-%s.txt'
                  % (ospfdb_path, router, port),
                  'w') as running_config_file:
            running_config_file.write(running_config)
        _routers = set()
        # Process route database
        with open('%s/route-detail-%s-%s.txt'
                  % (ospfdb_path, router, port), 'r') as route_file:
            # Process infos and get active routers
            for line in route_file:
                # Get a network prefix
                m = re.search('Destination: (\\S+)', line)
                if(m):
                    net = text_type(m.group(1))
                    continue
                # Get link-state id and the router advertising the network
                m = re.search('Intra-Prefix Id: (\\d*.\\d*.\\d*.\\d*) '
                              'Adv: (\\d*.\\d*.\\d*.\\d*)', line)
                if(m):
                    link_state_id = text_type(m.group(1))
                    adv_router = text_type(m.group(2))
                    # Add router to routers set
                    _routers.add(adv_router)
                    # It's a stub network or transit network
                    # Get the network id
                    # A network is uniquely identified by a pair
                    # (link state_id, advertising router)
                    network_id = (link_state_id, adv_router)
                    # Map network id to net ipv6 prefix
                    netid_to_netprefix[network_id] = net
                    if stub_networks.get(net) is None:
                        # Network is unknown, mark as a stub network
                        # Each network starts as a stub network,
                        # then it is processed and (eventually)
                        # marked as transit network
                        stub_networks[net] = set()
                    # adv_router can reach this net
                    stub_networks[net].add(adv_router)
        # Process network database
        transit_networks = dict()
        with open('%s/network-detail-%s-%s.txt'
                  % (ospfdb_path, router, port), 'r') as network_file:
            # Process infos and get active routers
            for line in network_file:
                # Get a link state id
                m = re.search('Link State ID: (\\d*.\\d*.\\d*.\\d*)', line)
                if(m):
                    link_state_id = text_type(m.group(1))
                    continue
                # Get the router advertising the network
                m = re.search('Advertising Router: (\\d*.\\d*.\\d*.\\d*)',
                              line)
                if(m):
                    # Get router ID of the router advertising the network
                    adv_router = text_type(m.group(1))
                    continue
                # Get routers directly connected to the network
                m = re.search('Attached Router: (\\d*.\\d*.\\d*.\\d*)', line)
                if(m):
                    # Get router ID of the router attached to the network
                    router_id = text_type(m.group(1))
                    # Get the network id: a network is uniquely identified
                    # by a pair (link state_id, advertising router)
                    network_id = (link_state_id, adv_router)
                    # Get net ipv6 prefix associated to this network
                    net = netid_to_netprefix.get(network_id)
                    if net is None:
                        # This network does not belong to route database
                        # This means that the network is no longer
                        # reachable (a router has been disconnected
                        # or an interface has been turned off)
                        continue
                    # Router can reach this net
                    stub_networks[net].add(router_id)
                    # Add advertising router to routers set
                    _routers.add(adv_router)
                    # Add attached router to routers set
                    _routers.add(router_id)
        # Process running config
        with open('%s/running-config-%s-%s.txt'
                  % (ospfdb_path, router, port),
                  'r') as running_config_file:
            # Process infos and get router id
            for line in running_config_file:
                # Get router id
                m = re.search('router-id (\\d*.\\d*.\\d*.\\d*)', line)
                if(m):
                    # Update mapping router id to router
                    routerid = text_type(m.group(1))
                    routerid_to_router[routerid] = router
                    break
        # Identify loopback nets
        for net in list(stub_networks):
            adv_router = list(stub_networks[net])[0]
            # Get the router ID
            _id = int(IPv4Address(adv_router))
            # Generate the loopback prefix of the router
            loopback_prefix = IPv6Network(int(IPv6Address
                                              (LOOPBACK_PREFIX)) | _id << 96)
            loopback_prefix = (IPv6Network(loopback_prefix)
                               .supernet(new_prefix=64))
            if IPv6Network(net).subnet_of(loopback_prefix):
                # The net is the loopback network of adv_router
                routerid_to_loopbacknet[adv_router] = net
                # The loopback IP address is the first address of the loopback
                # net
                loopbackip = str(next(IPv6Network(net)
                                      .hosts()))
                # Update mapping router ID to loopback IPs
                routerid_to_loopbackip[adv_router] = loopbackip
                # Remove it from stub networks
                del stub_networks[net]
        # Make separation between stub networks and transit networks
        for net, attached_routers in stub_networks.copy().items():
            if len(attached_routers) == 2:
                # Two routers connected to the network
                # Let's mark it as a transit network
                transit_networks[net] = attached_routers
                # and remove it from stub networks
                stub_networks.pop(net)
            elif len(attached_routers) == 1:
                # One router connected to the network
                # Let's mark it as a stub network
                stub_networks[net] = attached_routers
            elif len(stub_networks[net]) > 2:
                # A network cannot connect more than 2 routers
                print_and_die('Error: inconsistent network list')
        # Print results
        if verbose:
            print('*** Stub Networks: %s' % stub_networks.keys())
            print('*** Transit Networks: %s' % transit_networks.keys())
            print('*** Routers: %s' % _routers)
            print('**********************************************\n\n')
        # Build routers
        for routerid in _routers:
            loopbacknet = routerid_to_loopbacknet.get(routerid)
            loopbackip = routerid_to_loopbackip.get(routerid)
            routers_dict[routerid] = {
                'loopbacknet': loopbacknet,
                'loopbackip': loopbackip
            }
    # Done, return the topology graph
    return routers_dict, stub_networks, transit_networks


def topology_information_extraction(nodes, period, topo_file, topo_graph,
                                    ospf6d_pwd):
    # Topology Information Extraction
    while (True):
        # Extract the topology information
        routers, stub_networks, transit_networks = \
            connect_and_extract_topology(nodes, OSPF_DB_PATH, ospf6d_pwd)
        # Build the topology graph
        G = build_topo_graph(routers, stub_networks, transit_networks)
        # Dump relevant information of the network graph
        dump_topo(G, topo_file)
        # Export the network graph as an image file
        if topo_graph is not None:
            draw_topo(G, topo_graph)
        # Wait 'period' seconds between two extractions
        time.sleep(period)


# Utility function to export the network graph as an image file
def draw_topo(G, svg_topo_file, dot_topo_file=DOT_FILE_TOPO_GRAPH):
    # Create dot topology file, an intermediate representation
    # of the topology used to export as an image
    write_dot(G, dot_topo_file)
    os.system('dot -Tsvg %s -o %s' % (dot_topo_file, svg_topo_file))


# Utility function to dump relevant information of the topology
def dump_topo(G, topo_file):
    # Export NetworkX object into a json file
    # Json dump of the topology
    with open(topo_file, 'w') as outfile:
        # Get json topology
        json_topology = json_graph.node_link_data(G)
        # Convert links
        json_topology['links'] = [
            {
                'source_id': json_topology['nodes'][link['source']]['id'],
                'target_id': json_topology['nodes'][link['target']]['id'],
                'source': link['source'],
                'target': link['target'],
                'net': link['net']
            }
            for link in json_topology['links']]
        # Convert nodes
        json_topology['nodes'] = [
            {
                'id': node['id'],
                'routerid': node['routerid'],
                'loopbacknet':node['loopbacknet'],
                'loopbackip':node['loopbackip'],
                'type': node.get('type', '')
            }
            if node.get('type') == 'router'
            else  # stub network
            {
                'id': node['id'],
                'type': node.get('type', '')
            }
            for node in json_topology['nodes']]
        # Dump the topology
        json.dump(json_topology, outfile, sort_keys=True, indent=2)


# Parse command line options and dump results
def parseArguments():
    parser = ArgumentParser(
        description='Topology Information Extraction module for SRv6 '
        'Controller'
    )
    # ip:port of the routers
    parser.add_argument(
        '-n', '--node-ips', action='store', dest='nodes', required=True,
        help='Comma-separated <ip-port> pairs, where ip is the IP address of '
        'the router and port is the telnet port of the ospf6d daemon '
        '(e.g. 2000::1-2606,2000::2-2606,2000::3-2606)'
    )
    # Topology Information Extraction period
    parser.add_argument(
        '-p', '--period', dest='period', type=int,
        default=DEFAULT_TOPO_EXTRACTION_PERIOD,
        help='Polling period (in seconds)'
    )
    # Path of topology file
    parser.add_argument(
        '-t', '--topology', dest='topo_file', action='store',
        default=DEFAULT_TOPOLOGY_FILE, help='JSON file of the extracted '
        'topology'
    )
    # Path of topology graph
    parser.add_argument(
        '-g', '--topo-graph', dest='topo_graph', action='store', default=None,
        help='Image file of the exported NetworkX graph'
    )
    # Password used to log in to ospf6d daemon
    parser.add_argument(
        '-w', '--password', action='store_true', dest='password',
        default=DEFAULT_OSPF6D_PASSWORD, help='Password of the ospf6d daemon'
    )
    # Debug logs
    parser.add_argument(
        '-d', '--debug', action='store_true', help='Activate debug logs'
    )
    # Verbose mode
    parser.add_argument(
        '-v', '--verbose', action='store_true', dest='verbose', default=False,
        help='Enable verbose mode'
    )
    # Parse input parameters
    args = parser.parse_args()
    # Done, return
    return args


if __name__ == '__main__':
    global verbose
    # Let's parse input parameters
    args = parseArguments()
    # Setup properly the logger
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
    # Debug settings
    # SERVER_DEBUG = logger.getEffectiveLevel() == logging.DEBUG
    # logger.info('SERVER_DEBUG:' + str(SERVER_DEBUG))
    logging.basicConfig()
    logging.getLogger().setLevel(logging.DEBUG)
    # Get topology filename
    topo_file = args.topo_file
    # Get topology graph image filename
    topo_graph = args.topo_graph
    if topo_graph is not None and \
            not topo_graph.endswith('.svg'):
        # Add file extension
        topo_graph = '%s.%s' % (topo_graph, 'svg')
    # Nodes
    nodes = args.nodes
    nodes = nodes.split(',')
    # Get period between two extractions
    period = args.period
    # Verbose mode
    verbose = args.verbose
    # ospf6d password
    pwd = args.password
    # Deploy topology
    topology_information_extraction(nodes, period,
                                    topo_file, topo_graph, pwd)
