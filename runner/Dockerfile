FROM docker.io/library/python:3.8-buster

COPY ./bin/install.sh /usr/bin/provision
RUN /usr/bin/provision
RUN git clone -b v0.18.0 https://github.com/bitcoin/bitcoin.git /bitcoin
WORKDIR /bitcoin
RUN mkdir /bitcoin/data
ENV BDB_PREFIX /bitcoin/db4
RUN ./contrib/install_db4.sh . && \
  ./autogen.sh && \
  ./configure BDB_LIBS="-L${BDB_PREFIX}/lib -ldb_cxx-4.8" BDB_CFLAGS="-I${BDB_PREFIX}/include" && \
  make -j $(($(nproc) - 1))

WORKDIR /code
COPY setup.py /code/
COPY runner /code/runner
RUN pip install -r /code/runner/requirements.txt && \
  pip install -e . && \
  pip install -r /code/runner/requirements-dev.txt
