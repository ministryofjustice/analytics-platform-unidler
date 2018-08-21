FROM python:3.6.4-alpine AS base

MAINTAINER Andy Driver <andy.driver@digital.justice.gov.uk>

WORKDIR /home/unidler

RUN apk add --no-cache --virtual build-deps \
    build-base \
    libffi-dev \
    openssl-dev

ADD requirements.txt requirements.txt
RUN pip install -U pip && pip install -r requirements.txt

RUN apk del build-deps

ADD unidler.py unidler.py
ADD please_wait.html please_wait.html

CMD ["python", "unidler.py"]


FROM base AS test

RUN pip install pytest

ADD test test

RUN pytest test


FROM base
