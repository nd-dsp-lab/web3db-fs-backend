#!/bin/bash
# Reverse tunnel: exposes the IPFS swarm port (4001) on the EC2 proxy
# (18.227.51.47 — an Elastic IP, so it survives instance stop/start; the
# earlier auto-assigned address changed on restart and was recycled to a
# different AWS tenant), because the campus perimeter firewall blocks all
# inbound traffic to tjws-06. Public peers dial the EC2 address; kubo
# announces it via APPEND_ANNOUNCE_ADDR in ipfs/.env (see docker-compose.yml).
#
# The key is restricted on the EC2 side (restrict,port-forwarding,
# permitlisten="4001") — it can create this one forward and nothing else.
# TCP only: ssh cannot forward QUIC/UDP.
#
# Runs on tjws-06 from the user crontab (@reboot), log: ~/ipfs-tunnel.log
while true; do
  ssh -i ~/.ssh/id_ipfs_tunnel -N \
    -R 0.0.0.0:4001:127.0.0.1:4001 \
    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -o StrictHostKeyChecking=accept-new \
    ubuntu@18.227.51.47
  sleep 10
done
