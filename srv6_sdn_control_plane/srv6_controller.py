#!/usr/bin/python

from __future__ import print_function

# General imports
import configparser
import time
import json
import threading
import logging
from filelock import FileLock
from argparse import ArgumentParser
from threading import Thread
from threading import Lock
# ipaddress dependencies
from ipaddress import IPv6Interface
# NetworkX dependencies
import networkx as nx
from networkx.readwrite import json_graph
import srv6_sdn_control_plane.srv6_controller_utils as utils
# SRv6 dependencies
from srv6_sdn_control_plane.interface_discovery.interface_discovery import (
    interface_discovery
)
from srv6_sdn_control_plane.topology.ti_extraction import (
    draw_topo,
    connect_and_extract_topology
)
from srv6_sdn_control_plane.southbound.grpc.sb_grpc_client import (
    NetworkEventsListener
)
from srv6_sdn_control_plane.northbound.grpc import nb_grpc_server
from srv6_sdn_controller_state import srv6_sdn_controller_state
# pymerang dependencies
from pymerang.pymerang_server import PymerangController

# Global variables

# In our experiment we use srv6 as default password
DEFAULT_OSPF6D_PASSWORD = 'srv6'
# Default topology file
DEFAULT_TOPOLOGY_FILE = '/tmp/topology.json'
# Interval between two consecutive extractions (in seconds)
DEFAULT_TOPO_EXTRACTION_PERIOD = 10
# Dot file used to draw topology graph
DOT_FILE_TOPO_GRAPH = '/tmp/topology.dot'
# Default folder where to save extracted OSPF databases
OSPF_DB_PATH = '/tmp/ospf_db'
# Supported southbound interfaces
SUPPORTED_SB_INTERFACES = ['gRPC']
# Supported northbound interfaces
SUPPORTED_NB_INTERFACES = ['None', 'gRPC']
# Logger reference
logger = logging.getLogger(__name__)
# Server ip and port
DEFAULT_GRPC_SERVER_IP = '::'
DEFAULT_GRPC_SERVER_PORT = 54321
DEFAULT_GRPC_CLIENT_PORT = 12345
DEFAULT_PYMERANG_SERVER_IP = '::'
DEFAULT_PYMERANG_SERVER_PORT = 50061
# Debug option
SERVER_DEBUG = False
# Secure option
DEFAULT_SECURE = False
# Server certificate
DEFAULT_CERTIFICATE = 'cert_server.pem'
# Server key
DEFAULT_KEY = 'key_server.pem'
# Default southbound interface
DEFAULT_SB_INTERFACE = 'gRPC'
# Default northbound interface
DEFAULT_NB_INTERFACE = 'gRPC'
# Minimum interval between two topology dumps (in seconds)
DEFAULT_MIN_INTERVAL_BETWEEN_TOPO_DUMPS = 5
# Default interval between two keep alive messages
DEFAULT_KEEP_ALIVE_INTERVAL = 30


class SRv6Controller(object):

    def __init__(self, nodes, period, topo_file, topo_graph,
                 ospf6d_pwd, sb_interface, nb_interface,
                 nb_secure, sb_secure,
                 nb_server_key, nb_server_certificate,
                 sb_server_key, sb_server_certificate,
                 client_certificate, grpc_server_ip, grpc_server_port,
                 grpc_client_port, pymerang_server_ip,
                 pymerang_server_port, min_interval_between_topo_dumps,
                 topo_extraction=False,
                 keep_alive_interval=DEFAULT_KEEP_ALIVE_INTERVAL,
                 verbose=False):
        # Verbose mode
        self.VERBOSE = verbose
        if self.VERBOSE:
            print('*** Initializing controller variables')
        # Initialize variables
        # Nodes from which the topology has to be extracted
        self.nodes = nodes
        # Password used to log into ospf6d
        self.ospf6d_pwd = ospf6d_pwd
        # Period between two extractions
        self.period = period
        # Topology file
        self.topology_file = topo_file
        # Topology file
        self.topology_graph = topo_graph
        # Southbound interface
        self.sb_interface = sb_interface
        # Northbound interface
        self.nb_interface = nb_interface
        # Init lock for topology file
        self.topoFileLock = Lock()
        # Init Network Event Listener
        self.eventsListener = NetworkEventsListener()
        # Mapping router ID to listener thread
        self.listenerThreads = dict()
        # IP of the gRPC server
        self.grpc_server_ip = grpc_server_ip
        # Port of the gRPC server
        self.grpc_server_port = grpc_server_port
        # Port of the gRPC client
        self.grpc_client_port = grpc_client_port
        # IP of the pymerang server
        self.pymerang_server_ip = pymerang_server_ip
        # Port of the pymerang server
        self.pymerang_server_port = pymerang_server_port
        # Northbound secure mode
        self.nb_secure = nb_secure
        # Southbound secure mode
        self.sb_secure = sb_secure
        # Northbound server key
        self.nb_server_key = nb_server_key
        # Northbound server certificate
        self.nb_server_certificate = nb_server_certificate
        # Southbound server key
        self.sb_server_key = sb_server_key
        # Southbound server certificate
        self.sb_server_certificate = sb_server_certificate
        # Client certificate
        self.client_certificate = client_certificate
        # Graph
        self.G = nx.Graph()
        # Topology information
        self.topoInfo = {
            'routers': dict(),
            'nets': dict(),
            'interfaces': dict()
        }
        # Flag used to signal when the topology has changed
        self.topology_changed_flag = threading.Event()
        # Timestamp of the last topology dump
        self.last_dump_timestamp = 0
        # Lock used to protect topology graph data structure
        self.topo_graph_lock = Lock()
        # Minimum interval between dumps
        self.min_interval_between_topo_dumps = min_interval_between_topo_dumps
        # Topology information extraction
        self.topo_extraction = topo_extraction
        # Interval between two consecutive keep-alive messages
        self.keep_alive_interval = keep_alive_interval
        # Print configuration
        if self.VERBOSE:
            print()
            print('Configuration')
            print('*** Nodes: %s' % self.nodes)
            # print('*** ospf6d password: %s' % self.ospf6d_pwd)
            if self.topo_extraction:
                print('*** Topology Information Extraction: enabled')
                print(
                    '*** Topology Information Extraction period: %s' %
                    self.period
                )
                print('*** Topology file: %s' % self.topology_file)
                print('*** topology_graph: %s' % self.topology_graph)
            else:
                print('*** Topology Information Extraction: disabled')
            print('*** Selected southbound interface: %s' % self.sb_interface)
            print('*** Selected northbound interface: %s' % self.nb_interface)
            print()
        # Reference to the Northbound interface instance
        self.nb_interface_ref = None

    # Get the interface of the router facing on a net
    def get_interface_facing_on_net(self, routerid, net):
        interfaces = self.topoInfo['interfaces'].get(routerid)
        if interfaces is None:
            # The router does not exists
            # Topology has not changed, return False
            return None
        # Iterate on the interfaces
        for ifindex, ifdata in list(interfaces.items()):
            # Iterate on IP addresses
            for ipaddr in ifdata['ipaddr']:
                # Check if the IP address is in the net
                if utils.IPv6AddrInNet(ipaddr, net):
                    # Interface has been found
                    return ifdata
        # The interface does not exist
        return None

    # Get an address (loopback or management) for the router
    # The implementation of this method is different for
    # In-Band and Out-of-Band controller
    def get_router_address(self, routerid):
        pass

    ''' Router interfaces '''

    # Create/Update a router interface
    def create_router_interface(self, routerid, ifindex, ifname=None,
                                macaddr=None, ipaddr=None, state=None):
        # Create router if it does not exist
        if self.topoInfo['interfaces'].get(routerid) is None:
            self.topoInfo['interfaces'][routerid] = dict()
        # Create/Update a new interface
        if self.topoInfo['interfaces'][routerid].get(ifindex) is None:
            self.topoInfo['interfaces'][routerid][ifindex] = dict()
        # Get the interface
        interface = self.topoInfo['interfaces'][routerid][ifindex]
        # Set the interface name
        if ifname is not None:
            interface['ifname'] = ifname
        # Set the MAC address
        if macaddr is not None:
            interface['macaddr'] = macaddr
        # Set the IP address
        if ipaddr is not None:
            interface['ipaddr'] = list()
            interface['ipaddr'].append(ipaddr)
        # Set the state
        if state is not None:
            interface['state'] = state
        # The topology has changed, return True
        return True

    # Delete a router interface
    def delete_router_interface(self, routerid, ifindex):
        if self.topoInfo['interfaces'].get(routerid) is None:
            # The router does not exist
            # The topology has not changed
            return False
        if self.topoInfo['interfaces'][routerid].get(ifindex) is None:
            # The interface does not exist
            # The topology has not changed
            return False
        # Delete the interface
        del self.topoInfo['interfaces'][routerid][ifindex]
        # The topology has not changed
        return True

    ''' General helper functions for network-related data structures '''

    # Attach a router to an existing net
    def attach_router_to_net(self, routerid, net):
        # Attach the new router
        attached_routers = self.topoInfo['nets'].get(net)
        if attached_routers is None:
            # The network does not exist
            self.topoInfo['nets'][net] = set()
        self.topoInfo['nets'][net].add(routerid)
        # Topology has changed, return true
        return True

    # Detach a router from an existing net
    def detach_router_from_net(self, routerid, net):
        # Detach the router
        attached_routers = self.topoInfo['nets'].get(net)
        if attached_routers is None:
            # The network does not exist
            # Topology has not changed, return False
            return False
        if routerid not in attached_routers:
            # The router is not attached to the net
            # Topology has not changed, return False
            return False
        attached_routers.remove(routerid)
        # Topology has changed, return True
        return True

    # Detach a router interface from an existing net
    def detach_router_interface_from_net(self, routerid, ifindex):
        interfaces = self.topoInfo['interfaces'].get(routerid)
        if interfaces is None:
            # The router does not exist
            # Topology has not changed, return False
            return False
        if interfaces.get(ifindex) is None:
            # The interface does not exist
            # Topology has not changed, return False
            return False
        # Iterate on IP addresses
        topo_changed = False
        for ipaddr in interfaces[ifindex]['ipaddr']:
            # Detach router interface from net
            net = str(IPv6Interface(ipaddr).network)
            if self.detach_router_from_net(routerid, net):
                topo_changed = True
        # Return True if topo has changed, False otherwise
        return topo_changed

    ''' Manage detached networks '''

    # Get detached nets
    def get_detached_nets(self, new_nets):
        detached_nets = dict()
        old_nets = list(self.topoInfo['nets'].items())
        # Iterate on stub networks
        for net, attached_routers in old_nets:
            if net not in new_nets:
                # The network has been detached from the topology
                detached_nets[net] = attached_routers
            else:
                # The network still exists
                # Iterate on routers and get routers detached from the net
                for router in attached_routers:
                    if router not in new_nets[net]:
                        # Add detached routers to the net
                        if detached_nets.get(net) is None:
                            detached_nets[net] = set()
                        detached_nets[net].add(router)
        # Return the detached nets
        return detached_nets

    # Get new attached nets
    def get_attached_nets(self, new_nets):
        attached_nets = dict()
        # Iterate on new nets
        for net, attached_routers in list(new_nets.items()):
            old_attached_routers = self.topoInfo['nets'].get(net)
            if old_attached_routers is None:
                # The network does not existed before
                # New network
                attached_nets[net] = attached_routers
            else:
                # The network already existed before
                # Iterate on routers and get new routers attached to the net
                for router in attached_routers:
                    if router not in old_attached_routers:
                        # New attached router
                        if attached_nets.get(net) is None:
                            attached_nets[net] = set()
                        attached_nets[net].add(router)
        # Return the attached nets
        return attached_nets

    ''' Manage routers information '''

    # Get disconnected routers
    def get_disconnected_routers(self, new_routers):
        disconnected_routers = dict()
        old_routers = list(self.topoInfo['routers'].items())
        # Iterate on old routers
        for routerid, router_info in old_routers:
            if new_routers.get(routerid) is None:
                # The router is not present in the new topology
                # Router has been disconnected
                disconnected_routers[routerid] = router_info
        # Return disconnected routers set
        return disconnected_routers

    # Get changed routers
    def get_changed_routers(self, new_routers):
        changed_routers = dict()
        # Iterate on new routers
        for routerid, router_info in list(new_routers.items()):
            old_router = self.topoInfo['routers'].get(routerid)
            if router_info != old_router:
                # Router has changed
                changed_routers[routerid] = router_info
        # Return changed routers
        return changed_routers

    # Get new routers
    def get_new_routers(self, new_routers):
        _new_routers = dict()
        # Iterate on new routers
        for routerid, router_info in list(new_routers.items()):
            old_routers = self.topoInfo['routers']
            if routerid not in old_routers:
                # New router
                _new_routers[routerid] = router_info
        # Return the new routers
        return _new_routers

    # Add/Update routers to the topology
    def add_routers_to_topo(self, routers):
        # Iterate on routers
        for routerid, router_info in list(routers.items()):
            # Add the router to the topology information
            self.topoInfo['routers'][routerid] = router_info
        # Topology has changed, return True
        return True

    # Remove routers from the topology
    def remove_routers_from_topo(self, routers):
        topo_changed = False
        # Iterate on routers
        for routerid in routers:
            # Remove the router from the topology information
            if self.topoInfo['routers'].pop(routerid, None) is not None:
                topo_changed = True
        # Return True if the topology has changed, False otherwise
        return topo_changed

    ''' Manage interface information '''

    # Add an IP address to an interface
    def add_ipaddr_to_interface(self, routerid, ifindex, ipaddr):
        # Get router interfaces
        interfaces = self.topoInfo['interfaces'].get(routerid)
        if interfaces is None:
            # The router does not existxist
            # Topology has not changed
            return False
        if interfaces.get(ifindex):
            # The interface does not exist
            # Topology has not changed
            return False
        # Add the IP address to the interface
        self.topoInfo['interfaces'][routerid][ifindex]['ipaddr'].append(ipaddr)
        # Topology has changed
        return True

    # Remove an IP address from an interface
    def remove_ipaddr_from_interface(self, routerid, ifindex, ipaddr):
        # Get router interfaces
        interfaces = self.topoInfo['interfaces'].get(routerid)
        if interfaces is None:
            # The router does not existxist
            # Topology has not changed
            return False
        if interfaces.get(ifindex):
            # The interface does not exist
            # Topology has not changed
            return False
        if ipaddr not in interfaces[ifindex]['ipaddr']:
            # The IP address does not exist
            # Topology has not changed
            return False
        # Remove the IP address from the interface
        self.topoInfo['interfaces'][routerid][ifindex]['ipaddr'].remove(ipaddr)
        # Topology has changed
        return True

    ''' Interface discovery '''

    # Update the interfaces of the router by running interface discovery
    def discover_and_update_router_interfaces(self, routerid):
        if self.VERBOSE:
            print(('*** Extracting interface from router %s' % routerid))
        # Get the IP address of the router
        router = srv6_sdn_controller_state.get_device_mgmtip(deviceid=routerid)
        if router is None:
            # Router address not found
            # The topology has not changed
            return False
        # Discover the interfaces
        interfaces = interface_discovery(
            router, self.grpc_client_port, verbose=True
        )
        # Update topology information
        self.topoInfo['interfaces'][routerid] = interfaces
        # Return True if the operation completed successfully
        return interfaces is not None

    ''' Network Events Listener '''

    # Handle 'Interface UP' event
    # Return true if the topology has changed
    def handle_interface_up_event(self, routerid, ifindex, ifname, macaddr):
        topo_changed = False
        if self.create_router_interface(
            routerid=routerid,
            ifindex=ifindex,
            ifname=ifname,
            macaddr=macaddr,
            state='UP'
        ):
            topo_changed = True
        # Return True if the topology has changed, False otherwise
        return topo_changed

    # Handle 'Interface DOWN' event
    # Return true if the topology has changed
    def handle_interface_down_event(self, routerid, ifindex, ifname, macaddr):
        topo_changed = False
        # The interface does not exist, let's create a new one
        if self.create_router_interface(
            routerid=routerid,
            ifindex=ifindex,
            ifname=ifname,
            macaddr=macaddr,
            state='DOWN'
        ):
            topo_changed = True
        # Return True if the topology has changed, False otherwise
        return topo_changed

    # Handle 'Interface DEL' event
    # Return true if the topology has changed
    def handle_interface_del_event(self, routerid, ifindex):
        topo_changed = False
        # Detach the router from the net
        if self.detach_router_interface_from_net(routerid, ifindex):
            topo_changed = True
        # Let's remove the interface
        if self.delete_router_interface(routerid, ifindex):
            topo_changed = True
        # The topology has been updated
        return topo_changed

    # Handle 'NEW ADDR' event
    # Return true if the topology has changed
    def handle_new_addr_event(self, routerid, ifindex, ipaddr):
        topo_changed = False
        # Let's update the interface
        if self.add_ipaddr_to_interface(routerid, ifindex, ipaddr):
            topo_changed = True
        # Add the new network to the topology
        net = IPv6Interface(ipaddr).network
        if not net.is_link_local:
            # and attach the router to it
            if self.attach_router_to_net(routerid, str(net)):
                topo_changed = True
        # The topology has been updated
        return topo_changed

    # Handle 'DEL ADDR' event
    # Return true if the topology has changed
    def handle_del_addr_event(self, routerid, ifindex, ipaddr):
        topo_changed = False
        # Let's update the interface
        if self.remove_ipaddr_from_interface(routerid, ifindex, ipaddr):
            topo_changed = True
        # Get the network associated to the interface
        net = str(IPv6Interface(ipaddr).network)
        # and detach the router from it
        if self.detach_router_from_net(routerid, net):
            topo_changed = True
        # The topology has been updated
        return topo_changed

    # This function processes some network-related notifications
    # received from the nodes through the Southbound interface
    def listen_network_events(self, routerid):
        topo_changed = False
        router = srv6_sdn_controller_state.get_router_mgmtip(
            deviceid=routerid)
        if router is None:
            logging.warning(
                'Error in listen_network_events(): '
                'Cannot find an address for the router %s',
                router
            )
            return
        if self.VERBOSE:
            print('*** Listening network events from router %s' % router)
        if self.sb_interface == 'gRPC':
            # Wait for next network event notification and process it
            for event in self.eventsListener.listen(
                router, self.grpc_client_port
            ):
                if self.VERBOSE:
                    print(
                        '*** Received a new network event from router %s:\n%s'
                        % (routerid, event)
                    )
                if event['type'] == 'CONNECTION_ESTABLISHED':
                    # 'Connection established' message
                    # Run interface discovery to get missing information
                    # In the future, we don't need to run the interface
                    # discovery procedure anymore, since the SRv6 controller
                    # listens for network events from this node
                    if not self.discover_and_update_router_interfaces(
                        routerid
                    ):
                        # If the gRPC server on the node does not respond,
                        # abort and retry later
                        return
                    # Let's update the interfaces
                    for r, interfaces in \
                            list(self.topoInfo['interfaces'].items()):
                        for ifindex, ifdata in list(interfaces.items()):
                            for ipaddr in ifdata['ipaddr']:
                                # Add the new network to the topology
                                net = IPv6Interface(ipaddr).network
                                if not net.is_link_local:
                                    # and attach the router to it
                                    if self.attach_router_to_net(r, str(net)):
                                        topo_changed = True
                elif event['type'] == 'INTF_UP':
                    # Interface UP event
                    interface = event['interface']
                    # Extract interface index
                    ifindex = interface['index']
                    # Extract interface name
                    ifname = interface['name']
                    # Extract interface MAC address
                    macaddr = interface['macaddr']
                    # Handle interface up event
                    topo_changed = self.handle_interface_up_event(
                        routerid, ifindex, ifname, macaddr
                    )
                elif event['type'] == 'INTF_DOWN':
                    # Interface DOWN event
                    interface = event['interface']
                    # Extract interface index
                    ifindex = interface['index']
                    # Extract interface name
                    ifname = interface['name']
                    # Extract interface MAC address
                    macaddr = interface['macaddr']
                    # Handle interface down event
                    topo_changed = self.handle_interface_down_event(
                        routerid, ifindex, ifname, macaddr
                    )
                elif event['type'] == 'INTF_DEL':
                    # Interface DEL event
                    interface = event['interface']
                    # Extract interface index
                    ifindex = interface['index']
                    # Handle interface down event
                    topo_changed = self.handle_interface_del_event(
                        routerid, ifindex
                    )
                elif event['type'] == 'NEW_ADDR':
                    # New address event
                    interface = event['interface']
                    # Extract interface index
                    ifindex = interface['index']
                    # Extract address
                    ipaddr = interface['ipaddr']
                    # Handle new address event
                    topo_changed = self.handle_new_addr_event(
                        routerid, ifindex, ipaddr
                    )
                elif event['type'] == 'DEL_ADDR':
                    # Del address event
                    interface = event['interface']
                    # Extract interface index
                    ifindex = interface['index']
                    # Extract address
                    ipaddr = interface['ipaddr']
                    # Handle interface del event
                    topo_changed = self.handle_del_addr_event(
                        routerid, ifindex, ipaddr
                    )
                # Build, dump and draw topology, if it has changed
                if topo_changed:
                    if self.VERBOSE:
                        print('*** Topology has changed')
                    self.build_topo_graph()
        else:
            logging.error('Unsopported or invalid southbound interface')

    # Check if network events listener
    # is already started for a given router
    def check_network_events_listener(self, routerid):
        # Get the thread which handles the events listening
        listenerThread = self.listenerThreads.get(routerid)
        if listenerThread is not None and listenerThread.isAlive():
            # The thread is still alive
            return True
        else:
            # The thread is dead
            return False

    # Start network events listeners
    def start_network_events_listeners(self):
        # Iterate on routers
        for routerid, router_info in list(self.topoInfo['routers'].items()):
            if not self.check_network_events_listener(routerid):
                # The thread which handles the events listening is not active
                # Start a new thread events listener in a new thread
                thread = Thread(
                    name=routerid,
                    target=self.listen_network_events,
                    args=(routerid, )
                )
                thread.daemon = True
                # and update the mapping
                self.listenerThreads[routerid] = thread
                thread.start()

    ''' Topology Information Extraction '''

    # Utility function to dump relevant information of the topology
    def dump_topo(self, G):
        if self.VERBOSE:
            print(('*** Saving topology dump to %s' % self.topology_file))
        # Export NetworkX object into a json file
        # Json dump of the topology
        with FileLock(self.topology_file):
            with open(self.topology_file, 'w') as outfile:
                # Get json topology
                json_topology = json_graph.node_link_data(G)
                # Convert links
                json_topology['links'] = [
                    {
                        'source_id': json_topology['nodes'][
                            link['source']
                        ]['id'],
                        'target_id': json_topology['nodes'][
                            link['target']
                        ]['id'],
                        'source': link['source'],
                        'target': link['target'],
                        'source_ip': link['source_ip'],
                        'target_ip': link['target_ip'],
                        'source_intf': link['source_intf'],
                        'target_intf': link['target_intf'],
                        'net': link['net']
                    }
                    for link in json_topology['links']
                ]
                # Convert nodes
                json_topology['nodes'] = [
                    {
                        'id': node['id'],
                        'routerid': node['routerid'],
                        'loopbacknet': node['loopbacknet'],
                        'loopbackip': node['loopbackip'],
                        'managementip': node.get('managementip'),
                        'interfaces': node['interfaces'],
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

    # Build NetworkX Topology graph
    def build_topo_graph(self):
        if self.VERBOSE:
            print('*** Building topology')
            print(('*** Routers: %s' % list(self.topoInfo['routers'].keys())))
            print(('*** Nets: %s' % list(self.topoInfo['nets'].keys())))
        # Topology graph
        # G = nx.Graph()
        # Build topology graph
        with self.topo_graph_lock:
            # Remove all nodes and edges from the graph
            self.G.clear()
            # Build nodes list
            # Add routers to the graph
            for routerid, router_info in list(
                self.topoInfo['routers'].items()
            ):
                # Extract loopback net
                loopbacknet = router_info.get('loopbacknet')
                # Extract loopback IP
                loopbackip = router_info.get('loopbackip')
                # Extract management IP
                managementip = srv6_sdn_controller_state.get_router_mgmtip(
                    deviceid=routerid
                )
                # Extract router interfaces
                interfaces = self.topoInfo['interfaces'].get(routerid)
                # Add the node to the graph
                self.G.add_node(
                    routerid,
                    routerid=routerid,
                    fillcolor='red',
                    style='filled',
                    shape='ellipse',
                    loopbacknet=loopbacknet,
                    loopbackip=loopbackip,
                    managementip=managementip,
                    interfaces=interfaces,
                    type='router'
                )
            # Build edges list
            for net, routerids in list(self.topoInfo['nets'].items()):
                if len(routerids) == 2:
                    # Transit network
                    # Link between two routers
                    edge = list(routerids)
                    edge = (edge[0], edge[1])
                    # Get the two router interfaces corresponding to this edge
                    # Get interface name and IP address
                    # corresponding to the left router
                    lhs_intf = self.get_interface_facing_on_net(edge[0], net)
                    if lhs_intf is None or lhs_intf['state'] == 'DOWN':
                        # The interface is 'DOWN'
                        # Skip
                        continue
                    lhs_ifname = lhs_intf.get('ifname')
                    lhs_ip = utils.findIPv6AddrInNet(
                        lhs_intf.get('ipaddr'), net
                    )
                    # Get interface name and IP address
                    # corresponding to the right router
                    rhs_intf = self.get_interface_facing_on_net(edge[1], net)
                    if rhs_intf is None or rhs_intf['state'] == 'DOWN':
                        # The interface is 'DOWN'
                        # Skip
                        continue
                    rhs_ifname = rhs_intf.get('ifname')
                    rhs_ip = utils.findIPv6AddrInNet(
                        rhs_intf.get('ipaddr'), net
                    )
                    # Add edge to the graph
                    # This is a transit network, set the net as label
                    self.G.add_edge(
                        *edge,
                        label=net,
                        fontsize=9,
                        net=net,
                        source_ip=rhs_ip,
                        source_intf=rhs_ifname,
                        target_ip=lhs_ip,
                        target_intf=lhs_ifname
                    )
                elif len(routerids) == 1:
                    # Stub networks
                    # Link between a router and a stub network
                    edge = (list(routerids)[0], net)
                    # Get the interface of the left router
                    lhs_intf = self.get_interface_facing_on_net(edge[0], net)
                    if lhs_intf is None or lhs_intf['state'] == 'DOWN':
                        # The interface is 'DOWN'
                        # Skip
                        continue
                    lhs_ifname = lhs_intf.get('ifname')
                    lhs_ip = utils.findIPv6AddrInNet(
                        lhs_intf.get('ipaddr'), net
                    )
                    # Add a node representing the net to the graph
                    self.G.add_node(
                        net,
                        fillcolor='cyan',
                        style='filled',
                        shape='box',
                        type='stub_network'
                    )
                    # Add edge to the graph
                    # This is a stub network, no label on the edge
                    self.G.add_edge(
                        *edge,
                        label='',
                        fontsize=9,
                        net=net,
                        source_ip=None,
                        source_intf=None,
                        target_ip=lhs_ip,
                        target_intf=lhs_intf
                    )
        # Set the topology changed flag
        self.topology_changed_flag.set()

    def dump_and_draw_topo(self):
        while True:
            # Wait until the topology changes
            self.topology_changed_flag.wait()
            # Clear the topology changed flag
            self.topology_changed_flag.clear()
            if self.G is None:
                # No graph to draw
                continue
            # Wait for minimum interval between two topology dumps
            wait = self.last_dump_timestamp + \
                self.min_interval_between_topo_dumps - time.time()
            if wait > 0:
                time.sleep(wait)
            with self.topo_graph_lock:
                # Dump relevant information of the network graph
                self.dump_topo(self.G)
                # Export the network graph as an image file
                if self.topology_graph is not None:
                    draw_topo(self.G, self.topology_graph, DOT_FILE_TOPO_GRAPH)
            # Update last dump timestamp
            self.last_dump_timestamp = time.time()

    def update_topology_info(self, routers, nets):
        topo_changed = False
        # Get disconnected routers
        disconnected_routers = self.get_disconnected_routers(routers)
        # Get changed routers
        changed_routers = self.get_changed_routers(routers)
        # Get new routers
        new_routers = self.get_new_routers(routers)
        # Get detached nets
        detached_nets = self.get_detached_nets(nets)
        # Get attached nets
        attached_nets = self.get_attached_nets(nets)
        # Print results
        if self.VERBOSE:
            print(('*** New routers: %s' % new_routers))
            print(('*** Changed routers: %s' % changed_routers))
            print(('*** Disconnected routers: %s' % disconnected_routers))
        # Update the topology
        # Process disconnected routers
        if self.remove_routers_from_topo(disconnected_routers):
            topo_changed = True
        # Process new routers
        if self.add_routers_to_topo(new_routers):
            topo_changed = True
        # Process detached nets
        for net, routerids in list(detached_nets.items()):
            for routerid in routerids.copy():
                if self.detach_router_from_net(routerid, net):
                    topo_changed = True
        # Process attached nets
        for net, routerids in list(attached_nets.items()):
            for routerid in routerids.copy():
                if self.attach_router_to_net(routerid, net):
                    topo_changed = True
        # Return true if the topology has changed, False otherwise
        return topo_changed

    # Topology Information Extraction
    def topology_information_extraction(self):
        if self.VERBOSE:
            print('*** Starting Topology Information Extraction')
        # Loop
        stop = False
        while not stop:
            # Extract the topology from the routers
            routers, stub_nets, transit_nets = connect_and_extract_topology(
                self.nodes, OSPF_DB_PATH, self.ospf6d_pwd, True
            )
            nets = utils.merge_two_dicts(stub_nets, transit_nets)
            # Update the topology information
            if self.update_topology_info(routers, nets):
                # If topology has been updated
                # Build, dump and draw topology
                self.build_topo_graph()
            # Start receiving Netlink messages from new routers
            self.start_network_events_listeners()
            # Wait 'period' seconds between two extractions
            try:
                time.sleep(self.period)
            except KeyboardInterrupt:
                if self.VERBOSE:
                    print('*** Stopping Topology Information Extraction')
                stop = True
        if self.VERBOSE:
            print('*** Done')

    # Start registration server
    def start_registration_server(self):
        logging.info('*** Starting registration server')
        server = PymerangController(
            server_ip=self.pymerang_server_ip,
            server_port=self.pymerang_server_port,
            keep_alive_interval=self.keep_alive_interval,
            secure=self.sb_secure,
            key=self.sb_server_key,
            certificate=self.sb_server_certificate,
            nb_interface_ref=self.nb_interface_ref
        )
        server.serve()

    def start_nb_server(self, grpc_server_ip, grpc_server_port,
                        grpc_client_port, nb_secure, server_key,
                        server_certificate, sb_secure, client_certificate,
                        southbound_interface, topo_graph, vpn_dict, devices,
                        vpn_file, controller_state, verbose):
        nb_server, nb_interface_ref = nb_grpc_server.create_server(
            grpc_server_ip=grpc_server_ip,
            grpc_server_port=grpc_server_port,
            grpc_client_port=grpc_client_port,
            nb_secure=nb_secure,
            server_key=server_key,
            server_certificate=server_certificate,
            sb_secure=sb_secure,
            client_certificate=client_certificate,
            southbound_interface=southbound_interface,
            topo_graph=topo_graph,
            vpn_dict=vpn_dict,
            devices=devices,
            vpn_file=vpn_file,
            controller_state=controller_state,
            verbose=verbose
        )
        self.nb_interface_ref = nb_interface_ref
        # Start the loop for gRPC
        logging.info('Listening gRPC')
        nb_server.start()
        while True:
            time.sleep(5)

    # Run the SRv6 controller

    def run(self):
        if self.VERBOSE:
            print('*** Starting the SRv6 Controller')
        # Init database
        if srv6_sdn_controller_state.init_db() is not True:
            logging.error('Error while initializing database')
            return
        # Init Northbound Interface
        if self.nb_interface == 'gRPC':
            if self.VERBOSE:
                print('*** Starting gRPC Northbound server')
            # Start a new thread events listener in a new thread
            thread = Thread(
                target=self.start_nb_server,
                kwargs=(
                    {
                        'grpc_server_ip': self.grpc_server_ip,
                        'grpc_server_port': self.grpc_server_port,
                        'grpc_client_port': self.grpc_client_port,
                        'nb_secure': self.nb_secure,
                        'server_key': self.nb_server_key,
                        'server_certificate': self.nb_server_certificate,
                        'sb_secure': self.sb_secure,
                        'client_certificate': self.client_certificate,
                        'southbound_interface': self.sb_interface,
                        'topo_graph': self.G,
                        'verbose': self.VERBOSE,
                        'vpn_dict': None,
                        'vpn_file': None,
                        'devices': None,
                        'controller_state': None
                    }
                )
            )
            thread.daemon = True
            thread.start()
        # Start 'dump and draw' thread
        thread = Thread(
            target=self.dump_and_draw_topo
        )
        thread.daemon = True
        thread.start()
        # Wait for NB Interface getting ready
        time.sleep(3)
        # Start registration server
        thread = Thread(
            target=self.start_registration_server
        )
        thread.daemon = True
        thread.start()
        # Start topology information extraction
        if self.topo_extraction:
            self.topology_information_extraction()
        while True:
            time.sleep(100)


# Parse arguments
def parseArguments():
    # Get parser
    parser = ArgumentParser(description='SRv6 Controller')
    # Node IP-PORTs mapping
    parser.add_argument(
        '--ips',
        action='store',
        dest='nodes',
        help='IP of the routers from which the topology has to be extracted, '
        'comma-separated IP-PORT maps '
        '(i.e. 2000::1-2606,2000::2-2606,2000::3-2606)'
    )
    # Topology Information Extraction period
    parser.add_argument(
        '-p', '--period',
        dest='period',
        type=int,
        default=DEFAULT_TOPO_EXTRACTION_PERIOD,
        help='Topology information extraction period'
    )
    # Path of topology file
    parser.add_argument(
        '--topology',
        dest='topo_file',
        action='store',
        default=DEFAULT_TOPOLOGY_FILE,
        help='File where the topology extracted has to be saved'
    )
    # Path of topology graph
    parser.add_argument(
        '--topo-graph',
        dest='topo_graph',
        action='store',
        default=None,
        help='File where the topology graph image has to be saved'
    )
    # Enable debug logs
    parser.add_argument(
        '-d',
        '--debug',
        action='store_true',
        help='Activate debug logs'
    )
    # Password used to log in to ospf6d daemon
    parser.add_argument(
        '--password',
        action='store_true',
        dest='password',
        default=DEFAULT_OSPF6D_PASSWORD,
        help='Password used to log in to ospf6d daemon'
    )
    # Verbose mode
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        dest='verbose',
        default=False,
        help='Enable verbose mode'
    )
    # Enable topology information extraction
    parser.add_argument(
        '-t',
        '--topo-extraction',
        action='store_true',
        dest='topo_extraction',
        default=False,
        help='Enable topology information extraction'
    )
    # Southbound interface
    parser.add_argument(
        '--sb-interface',
        action='store',
        dest='sb_interface',
        default=DEFAULT_SB_INTERFACE,
        help='Select a southbound interface from this list: %s'
        % SUPPORTED_SB_INTERFACES
    )
    # Northbound interface
    parser.add_argument(
        '--nb-interface',
        action='store',
        dest='nb_interface',
        default=DEFAULT_NB_INTERFACE,
        help='Select a northbound interface from this list: %s'
        % SUPPORTED_NB_INTERFACES
    )
    # IP address of the northbound gRPC server
    parser.add_argument(
        '--grpc-server-ip',
        dest='grpc_server_ip',
        action='store',
        default=DEFAULT_GRPC_SERVER_IP,
        help='IP of the northbound gRPC server'
    )
    # Port of the northbound gRPC server
    parser.add_argument(
        '--grpc-server-port',
        dest='grpc_server_port',
        action='store',
        default=DEFAULT_GRPC_SERVER_PORT,
        help='Port of the northbound gRPC server'
    )
    # Port of the northbound gRPC client
    parser.add_argument(
        '--grpc-client-port',
        dest='grpc_client_port',
        action='store',
        default=DEFAULT_GRPC_CLIENT_PORT,
        help='Port of the northbound gRPC client'
    )
    # IP address of the pymerang server
    parser.add_argument(
        '--pymerang-server-ip',
        dest='pymerang_server_ip',
        action='store',
        default=DEFAULT_PYMERANG_SERVER_IP,
        help='IP of the pymerang server'
    )
    # Port of the pymerang server
    parser.add_argument(
        '--pymerang-server-port',
        dest='pymerang_server_port',
        action='store',
        default=DEFAULT_PYMERANG_SERVER_PORT,
        help='Port of the pymerang server'
    )
    # Enable secure mode for the northbound interface
    parser.add_argument(
        '-s',
        '--nb-secure',
        action='store_true',
        dest='nb_secure',
        default=DEFAULT_SECURE,
        help='Activate secure mode for the northbound interface'
    )
    # Enable secure mode for the southboun interface
    parser.add_argument(
        '-x',
        '--sb-secure',
        action='store_true',
        dest='sb_secure',
        default=DEFAULT_SECURE,
        help='Activate secure mode for the southbound interface'
    )
    # Server certificate
    parser.add_argument(
        '--nb-server-cert',
        dest='nb_server_cert',
        action='store',
        default=DEFAULT_CERTIFICATE,
        help='Northbound server certificate file'
    )
    # Server key
    parser.add_argument(
        '--nb-server-key',
        dest='nb_server_key',
        action='store',
        default=DEFAULT_KEY,
        help='Northbound server key file'
    )
    # Server certificate
    parser.add_argument(
        '--sb-server-cert',
        dest='sb_server_cert',
        action='store',
        default=DEFAULT_CERTIFICATE,
        help='Southbound server certificate file'
    )
    # Server key
    parser.add_argument(
        '--sb-server-key',
        dest='sb_server_key',
        action='store',
        default=DEFAULT_KEY,
        help='Southbound server key file'
    )
    # Client certificate
    parser.add_argument(
        '--client-cert',
        dest='client_cert',
        action='store',
        default=DEFAULT_CERTIFICATE,
        help='Client certificate file'
    )
    # Port of the northbound gRPC client
    parser.add_argument(
        '--min-interval-dumps',
        dest='min_interval_between_topo_dumps',
        action='store',
        default=DEFAULT_MIN_INTERVAL_BETWEEN_TOPO_DUMPS,
        help='Minimum interval between two consecutive dumps'
    )
    # Config file
    parser.add_argument(
        '-c',
        '--config-file',
        dest='config_file',
        action='store',
        default=None,
        help='Path of the configuration file'
    )
    # Config file
    parser.add_argument(
        '--keep-alive-interval',
        dest='keep_alive_interval',
        action='store',
        default=DEFAULT_KEEP_ALIVE_INTERVAL,
        help='Interval between two consecutive keep alive messages'
    )
    # Parse input parameters
    args = parser.parse_args()
    # Done, return
    return args


# Parse a configuration file
def parse_config_file(config_file):

    class Args:
        nodes = None
        period = None
        topo_file = None
        topo_graph = None
        debug = None
        password = None
        verbose = None
        topo_extraction = None
        sb_interface = None
        nb_interface = None
        grpc_server_ip = None
        grpc_server_port = None
        grpc_client_port = None
        pymerang_server_ip = None
        pymerang_server_port = None
        nb_secure = None
        sb_secure = None
        nb_server_cert = None
        nb_server_key = None
        sb_server_cert = None
        sb_server_key = None
        client_cert = None
        min_interval_between_topo_dumps = None
        keep_alive_interval = None

    args = Args()
    # Get parser
    config = configparser.ConfigParser()
    # Read configuration file
    config.read(config_file)
    # Node IP-PORTs mapping
    args.nodes = config['DEFAULT'].get('nodes')
    if args.nodes is None:
        print('Missing required argument nodes')
        exit()
    # Topology Information Extraction period
    args.period = config['DEFAULT'].get(
        'period', DEFAULT_TOPO_EXTRACTION_PERIOD
    )
    # Path of topology file
    args.topo_file = config['DEFAULT'].get('topo_file', DEFAULT_TOPOLOGY_FILE)
    # Path of topology graph
    args.topo_graph = config['DEFAULT'].get('topo_graph', None)
    # Enable debug logs
    args.debug = config['DEFAULT'].get('debug', False)
    # Password used to log in to ospf6d daemon
    args.password = config['DEFAULT'].get('password', DEFAULT_OSPF6D_PASSWORD)
    # Verbose mode
    args.verbose = config['DEFAULT'].get('verbose', False)
    # Enable topology information extraction
    args.topo_extraction = config['DEFAULT'].get('topo_extraction', False)
    # Southbound interface
    args.sb_interface = config['DEFAULT'].get(
        'sb_interface', DEFAULT_SB_INTERFACE
    )
    # Northbound interface
    args.nb_interface = config['DEFAULT'].get(
        'nb_interface', DEFAULT_NB_INTERFACE
    )
    # IP address of the northbound gRPC server
    args.grpc_server_ip = config['DEFAULT'].get(
        'grpc_server_ip', DEFAULT_GRPC_SERVER_IP
    )
    # Port of the northbound gRPC server
    args.grpc_server_port = config['DEFAULT'].get(
        'grpc_server_port', DEFAULT_GRPC_SERVER_PORT
    )
    # Port of the northbound gRPC client
    args.grpc_client_port = config['DEFAULT'].get(
        'grpc_client_port', DEFAULT_GRPC_CLIENT_PORT
    )
    # IP address of the pymerang server
    args.pymerang_server_ip = config['DEFAULT'].get(
        'pymerang_server_ip', DEFAULT_PYMERANG_SERVER_IP
    )
    # Port of the pymerang server
    args.pymerang_server_port = config['DEFAULT'].get(
        'pymerang_server_port', DEFAULT_PYMERANG_SERVER_PORT
    )
    # Enable secure mode for the northbound interface
    args.nb_secure = config['DEFAULT'].get('nb_secure', DEFAULT_SECURE)
    # Enable secure mode for the southbound interface
    args.sb_secure = config['DEFAULT'].get('sb_secure', DEFAULT_SECURE)
    # Server certificate
    args.nb_server_cert = config['DEFAULT'].get(
        'nb_server_cert', DEFAULT_CERTIFICATE
    )
    # Server key
    args.nb_server_key = config['DEFAULT'].get('nb_server_key', DEFAULT_KEY)
    # Server certificate
    args.sb_server_cert = config['DEFAULT'].get(
        'sb_server_cert', DEFAULT_CERTIFICATE
    )
    # Server key
    args.sb_server_key = config['DEFAULT'].get('sb_server_key', DEFAULT_KEY)
    # Client certificate
    args.client_cert = config['DEFAULT'].get(
        'client_cert', DEFAULT_CERTIFICATE
    )
    # Port of the northbound gRPC client
    args.min_interval_between_topo_dumps = config['DEFAULT'].get(
        'min_interval_between_topo_dumps',
        DEFAULT_MIN_INTERVAL_BETWEEN_TOPO_DUMPS
    )
    # Keep-alive interval
    args.keep_alive_interval = config['DEFAULT'].get(
        'keep_alive_interval', DEFAULT_KEEP_ALIVE_INTERVAL
    )
    # Done, return
    return args


def _main():
    # Let's parse input parameters
    args = parseArguments()
    # Check if a configuration file has been provided
    if args.config_file is not None:
        args = parse_config_file(args.config_file)
    # Get topology filename
    topo_file = args.topo_file
    # Get topology graph image filename
    topo_graph = args.topo_graph
    if topo_graph is not None and not topo_graph.endswith('.svg'):
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
    # Southbound interface
    sb_interface = args.sb_interface
    # Northbound interface
    nb_interface = args.nb_interface
    # Topology Information Extraction
    topo_extraction = args.topo_extraction
    # Setup properly the logger
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger().setLevel(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)
        logging.getLogger().setLevel(level=logging.INFO)
    # Setup properly the secure mode
    if args.nb_secure:
        nb_secure = True
    else:
        nb_secure = False
    # Setup properly the secure mode
    if args.sb_secure:
        sb_secure = True
    else:
        sb_secure = False
    # gRPC server IP
    grpc_server_ip = args.grpc_server_ip
    # gRPC server port
    grpc_server_port = args.grpc_server_port
    # gRPC client port
    grpc_client_port = args.grpc_client_port
    # pymerang server IP
    pymerang_server_ip = args.pymerang_server_ip
    # pymerang server port
    pymerang_server_port = args.pymerang_server_port
    # Northbound server certificate
    nb_server_certificate = args.nb_server_cert
    # Northbound server key
    nb_server_key = args.nb_server_key
    # Southbound server certificate
    sb_server_certificate = args.sb_server_cert
    # Server key
    sb_server_key = args.sb_server_key
    # Client certificate
    client_certificate = args.client_cert
    # Minimum interval between two consecutive topology dumps
    min_interval_between_topo_dumps = args.min_interval_between_topo_dumps
    # Keep-alive interval
    keep_alive_interval = args.keep_alive_interval
    SERVER_DEBUG = logger.getEffectiveLevel() == logging.DEBUG
    logger.info('SERVER_DEBUG:' + str(SERVER_DEBUG))
    # Check interfaces file, dataplane and gRPC client paths
    if sb_interface not in SUPPORTED_SB_INTERFACES:
        utils.print_and_die(
            'Error: %s interface not yet supported or invalid\n'
            'Supported southbound interfaces: %s' %
            (sb_interface, SUPPORTED_SB_INTERFACES)
        )
    if nb_interface not in SUPPORTED_NB_INTERFACES:
        utils.print_and_die(
            'Error: %s interface not yet supported or invalid\n'
            'Supported northbound interfaces: %s' %
            (nb_interface, SUPPORTED_NB_INTERFACES)
        )
    # Create a new SRv6 controller
    srv6_controller = SRv6Controller(
        nodes=nodes,
        period=period,
        topo_file=topo_file,
        topo_graph=topo_graph,
        ospf6d_pwd=pwd,
        sb_interface=sb_interface,
        nb_interface=nb_interface,
        nb_secure=nb_secure,
        sb_secure=sb_secure,
        nb_server_key=nb_server_key,
        nb_server_certificate=nb_server_certificate,
        sb_server_key=sb_server_key,
        sb_server_certificate=sb_server_certificate,
        client_certificate=client_certificate,
        grpc_server_ip=grpc_server_ip,
        grpc_server_port=grpc_server_port,
        grpc_client_port=grpc_client_port,
        pymerang_server_ip=pymerang_server_ip,
        pymerang_server_port=pymerang_server_port,
        min_interval_between_topo_dumps=min_interval_between_topo_dumps,
        topo_extraction=topo_extraction,
        keep_alive_interval=keep_alive_interval,
        verbose=verbose
    )
    # Start the controller
    srv6_controller.run()


if __name__ == '__main__':
    _main()
