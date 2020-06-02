#!/bin/bash

if ! which apt >/dev/null; then
  echo "Requires debian-like system"
fi

SUDO=""
if which sudo > /dev/null; then
  SUDO="sudo "
fi

echo "Installing bitcoin core dependencies"
$SUDO apt-get update -qq
DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -qq -y \
  libfreetype6-dev \
  build-essential libtool autotools-dev automake \
  pkg-config libssl-dev libevent-dev bsdmainutils ccache libqt5gui5 \
  libqt5core5a libqt5dbus5 qttools5-dev qttools5-dev-tools libprotobuf-dev \
  protobuf-compiler libboost-system-dev libboost-filesystem-dev \
  libboost-chrono-dev libboost-program-options-dev libboost-test-dev \
  libboost-thread-dev \
  clang \
  git wget time python3-dev python3-pip

if ! which python3.8; then
  PY_VERSION=3.8.3
  echo "Installing Python $PY_VERSION"
  $SUDO apt update -qq
  $SUDO apt-get install -y -qq \
    build-essential zlib1g-dev libncurses5-dev libgdbm-dev \
    libnss3-dev libssl-dev libreadline-dev libffi-dev curl
  cd /tmp
  curl -O https://www.python.org/ftp/python/${PY_VERSION}/Python-${PY_VERSION}.tar.xz
  tar -xf Python-${PY_VERSION}.tar.xz
  cd Python-${PY_VERSION}
  ./configure --enable-optimizations
  make -j $(nproc)
  $SUDO make altinstall
  $SUDO rm -rf /tmp/Python-${PY_VERSION}*
fi

if ! which fio; then
  echo "Installing fio for IO testing (bitcoinperf-hwinfo)"
  $SUDO apt install -y -qq fio
fi

if [ -f "./setup.py" ]; then
  python3.8 -m pip install --user -e .
fi
