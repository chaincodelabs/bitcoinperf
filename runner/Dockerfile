FROM debian:9-slim

COPY provision /usr/bin/provision
RUN /usr/bin/provision
RUN git clone -b v0.16.1 https://github.com/bitcoin/bitcoin.git /bitcoin
WORKDIR /bitcoin
RUN mkdir /bitcoin/data
ENV BDB_PREFIX /bitcoin/db4
RUN ./contrib/install_db4.sh . && \
  ./autogen.sh && \
  ./configure BDB_LIBS="-L${BDB_PREFIX}/lib -ldb_cxx-4.8" BDB_CFLAGS="-I${BDB_PREFIX}/include" && \
  make -j $(($(nproc) - 1))

WORKDIR /code
COPY * /code/
RUN pip3 install -r requirements.txt
