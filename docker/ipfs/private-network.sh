#!/bin/sh
set -e
# Private swarm: never dial the public bootstrap peers.
# Peers are connected to each other by their cluster sidecars.
ipfs bootstrap rm --all
ipfs config --json AutoConf.Enabled false