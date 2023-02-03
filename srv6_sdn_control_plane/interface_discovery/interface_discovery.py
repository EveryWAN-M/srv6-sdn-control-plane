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
# Interface names discovery
#
# @author Carmine Scarpitta <carmine.scarpitta.94@gmail.com>
# @author Pier Luigi Ventre <pier.luigi.ventre@uniroma2.it>
# @author Stefano Salsano <stefano.salsano@uniroma2.it>
#


# General imports
from argparse import ArgumentParser
import grpc
import json
import os
import pprint
import sys
import errno

# Path to the southbound gRPC-based client
# #SB_GRPC_CLIENT_PATH = '../southbound/grpc'

# Adjust relative paths
# script_path = os.path.dirname(os.path.abspath(__file__))
# SB_GRPC_CLIENT_PATH = os.path.join(script_path, SB_GRPC_CLIENT_PATH)

# Add path of gRPC APIs
# sys.path.append(SB_GRPC_CLIENT_PATH)

# SRv6 dependencies
from srv6_sdn_control_plane.southbound.grpc.sb_grpc_client import SRv6Manager

# Interfaces file
DEFAULT_INTERFACES_FILE = '/tmp/interfaces.json'
# Verbose mode
DEFAULT_VERBOSE = True
# Supported southbound interfaces
SUPPORTED_SB_INTERFACES = ['gRPC']


def interface_discovery(router, port, verbose=False):
    # Get a reference to SRv6Manager
    srv6_manager = SRv6Manager()
    # Extract interfaces
    if verbose:
        print(
            '\n*********** Extracting interfaces from %s ***********' % router
        )
    try:
        interfaces = srv6_manager.get_interface(router, port)
    except grpc.RpcError as e:
        if e.details() == 'Connect Failed' and verbose:
            print('Cannot connect to %s' % router)
        return None
    # Print interfaces
    if verbose:
        pp = pprint.PrettyPrinter()
        pp.pprint(interfaces)
    return interfaces


def interface_discovery_many(routers, verbose=False):
    # Get a reference to SRv6Manager
    # srv6_manager = SRv6Manager()
    # Mapping router to interfaces list
    router_to_interfaces = dict()
    # Extract interfaces
    for router in routers:
        # Separate IP and port
        data = router.split('-')
        router = data[0]
        port = data[1]
        router_to_interfaces[router] = interface_discovery(
            router, port, verbose)
    # Print interfaces
    if verbose:
        pp = pprint.PrettyPrinter()
        pp.pprint(router_to_interfaces)
    return router_to_interfaces


# Utility function to dump relevant information of the interfaces
def dump_interfaces(interfaces, output_filename):
    if output_filename is None or output_filename == '':
        print('Error: no valid output_filename provided to dump_interfaces()')
        return
    # Check if the parent folder of DEFAULT_INTERFACES_FILE exists, if not
    # create it
    if not os.path.exists(os.path.dirname(output_filename)):
        try:
            os.makedirs(os.path.dirname(output_filename))
        except OSError as exc:  # This workaround will avoid a race condition
            if exc.errno != errno.EEXIST:
                raise
    # Export interfaces into a json file
    # Json dump of the interfaces
    with open(output_filename, 'w') as outfile:
        # Dump the ips of the interfaces
        json.dump(interfaces, outfile, sort_keys=True, indent=2)


# Parse command line options and dump results
def parseArguments():
    # Get parser
    parser = ArgumentParser(
        description='Interface discovery module for SRv6 Controller'
    )
    # ip of the routers
    parser.add_argument(
        '-n', '--node-ips', action='store', dest='nodes', required=True,
        help='Comma-separated <ip-port> pairs, where ip is the IP address of '
        'the router and port is the port of the gRPC server '
        '(e.g. 2000::1-12345,2000::2-12345,2000::3-12345)'
    )
    # Path of interfaces file
    parser.add_argument(
        '-i', '--interfaces', dest='interfaces_file', action='store',
        default=DEFAULT_INTERFACES_FILE, help='JSON file of the extracted '
        'interfaces'
    )
    # Southbound interface
    parser.add_argument(
        '-s', '--sb-interface', action='store', dest='sb_interface',
        default='grpc',
        help='Southbound interface used to interact with the nodes, chosen '
        'from this list [grpc]'
    )
    # Verbose mode
    parser.add_argument(
        '-v', '--verbose', action='store_true', dest='verbose',
        default=DEFAULT_VERBOSE, help='Enable verbose mode'
    )
    # Parse input parameters
    args = parser.parse_args()
    # Done, return
    return args


if __name__ == '__main__':
    global VERBOSE
    # Let's parse input parameters
    args = parseArguments()
    # Get interfaces filename
    interfaces_file = args.interfaces_file
    # Nodes
    nodes = args.nodes
    nodes = nodes.split(',')
    # VERBOSE mode
    verbose = args.verbose
    # Southbound interface
    SOUTHBOUND_INTERFACE = args.sb_interface
    # Check interfaces file, dataplane and gRPC client paths
    if SOUTHBOUND_INTERFACE not in SUPPORTED_SB_INTERFACES:
        print('Error: %s interface not yet supported or invalid\n'
              'Supported southbound interfaces: %s' % SUPPORTED_SB_INTERFACES)
        sys.exit(-2)
    # Extract interface info
    if len(nodes) > 0:
        # Discover interfaces
        router_to_interfaces = interface_discovery_many(nodes, verbose)
        # Dump relevant information of the interfaces
        dump_interfaces(router_to_interfaces, interfaces_file)
    else:
        print('No router selected')
        sys.exit(-2)
    '''
    # Discover interfaces
    interfaces = interface_discovery(routers[0])
    # Dump relevant information of the interfaces
    dump_interfaces(interfaces, DEFAULT_INTERFACES_FILE)
    '''
