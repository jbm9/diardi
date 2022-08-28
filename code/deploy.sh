#!/bin/sh

SERVICE_NAME=diardi.service

sudo cp -v ./diardi.py /usr/local/bin

sudo cp -v ./${SERVICE_NAME} /lib/systemd/system/
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl restart ${SERVICE_NAME}
