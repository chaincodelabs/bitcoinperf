FROM ubuntu:18.04

RUN apt-get update && \
  apt-get install -y linux-tools-4.15.0-22-generic && \
  apt-get install -y git build-essential libtool autotools-dev \
  automake pkg-config libssl-dev libevent-dev bsdmainutils ccache libqt5gui5 \
  libqt5core5a libqt5dbus5 qttools5-dev qttools5-dev-tools libprotobuf-dev \
  protobuf-compiler libboost-system-dev libboost-filesystem-dev \
  libboost-chrono-dev libboost-program-options-dev libboost-test-dev \
  libboost-thread-dev
 
# run_bench.py dependencies
RUN apt-get install -y curl wget time python3-dev python3-pip && \
  apt-get autoremove
                
RUN git clone -b v0.16.1 https://github.com/bitcoin/bitcoin.git /bitcoin
WORKDIR /bitcoin
ENV BDB_PREFIX /bitcoin/db4
RUN ./contrib/install_db4.sh . && \
  ./autogen.sh && \
  ./configure BDB_LIBS="-L${BDB_PREFIX}/lib -ldb_cxx-4.8" BDB_CFLAGS="-I${BDB_PREFIX}/include" && \
  make -j $(($(nproc) - 1))

WORKDIR /code
COPY * /code/
RUN pip3 install -r requirements.txt
