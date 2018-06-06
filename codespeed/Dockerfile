FROM python:3.6.3
ENV PYTHONUNBUFFERED 1
RUN mkdir /code && mkdir /repos
RUN git clone https://github.com/bitcoin/bitcoin.git /repos/bitcoin
WORKDIR /code
COPY requirements.txt /code/
RUN pip install -r requirements.txt
COPY . /code/
