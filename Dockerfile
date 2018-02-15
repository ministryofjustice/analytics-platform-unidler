FROM python:3.6.4-alpine

MAINTAINER Andy Driver <andy.driver@digital.justice.gov.uk>

WORKDIR /home/unidler

ADD unidler.py unidler.py

CMD ["python", "unidler.py"]
