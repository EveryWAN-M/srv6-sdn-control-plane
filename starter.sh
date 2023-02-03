#!/bin/bash

source /home/user/Envs/srv6env/bin/activate
python ./srv6_controller.py --verbose --ips 2000::1-2606,2000::2-2606,2000::3-2606 --period 3