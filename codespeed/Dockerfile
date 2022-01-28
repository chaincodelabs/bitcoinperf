FROM docker.io/library/python:3.6.3
ENV PYTHONUNBUFFERED 1
RUN mkdir /code && mkdir /repos && git clone https://github.com/bitcoin/bitcoin.git /repos/bitcoin
WORKDIR /code
COPY Pipfile Pipfile.lock /code/
RUN pip install pipenv==2018.11.26 && pipenv install --system
ENTRYPOINT ["/code/docker_entrypoint"]
