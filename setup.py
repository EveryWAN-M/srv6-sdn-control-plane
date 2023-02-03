# Always prefer setuptools over distutils
from setuptools import setup
# To use a consistent encoding
from codecs import open
from os import path

here = path.abspath(path.dirname(__file__))

# Get the long description from the README file
with open(path.join(here, 'README.md'), encoding='utf-8') as f:
    long_description = f.read()

# Read version from VERSION file
with open(path.join(here, 'VERSION')) as version_file:
    version = version_file.read().strip()

# Arguments marked as "Required" below must be included for upload to PyPI.
# Fields marked as "Optional" may be commented out.
setup(
    name='srv6-sdn-control-plane',
    version=version,
    description='SRv6 SDN Control Plane',  # Required
    long_description=long_description,
    long_description_content_type='text/markdown',  # Optional (see note above)
    entry_points={
        'console_scripts': [
            'srv6_controller = srv6_sdn_control_plane.srv6_controller:_main'
        ]
    },
    url='',  # Optional
    packages=[
        'srv6_sdn_control_plane',
        'srv6_sdn_control_plane.interface_discovery',
        'srv6_sdn_control_plane.topology',
        'srv6_sdn_control_plane.northbound',
        'srv6_sdn_control_plane.northbound.grpc',
        'srv6_sdn_control_plane.southbound',
        'srv6_sdn_control_plane.southbound.grpc',
        'srv6_sdn_control_plane.southbound.netconf',
        'srv6_sdn_control_plane.southbound.rest',
        'srv6_sdn_control_plane.southbound.ssh'
    ],  # Required
    install_requires=[
        'setuptools',
        'grpcio>=1.19.0',
        'grpcio-tools>=1.19.0',
        'ipaddress>=1.0.22',
        'networkx==1.11',
        'protobuf>=3.7.1',
        'pygraphviz>=1.5',
        'six>=1.12.0',
        'sshutil>=1.5.0',
        'filelock>=3.0.12',
        'cffi>=1.14.1',
        'cryptography>=3.0',
        'pycparser>=2.20',
        'bcrypt>=3.1.7',
        'pynacl>=1.4.0',
        # 'rollbackcontext==0.1.post2',
        'rollbackcontext'
    ],
    dependency_links=[
        'git+https://github.com/cscarpitta/rollbackcontext@porting-to-python3#egg=rollbackcontext'  # noqa E501
    ]
)
