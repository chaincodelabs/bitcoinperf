#!/bin/bash


if [ $# -lt "2" ]; then
    echo "Usage: <datadir-path-to-compress> <name-of-output>"
    exit 1
fi

tar -czvf ${2}.tar.gz -C ${1} .
