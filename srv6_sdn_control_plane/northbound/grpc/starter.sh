#!/bin/bash

source /home/user/Envs/srv6env/bin/activate
python ./nb_grpc_server.py --ips 2000::1,2000::2,2000::3
sleep 10