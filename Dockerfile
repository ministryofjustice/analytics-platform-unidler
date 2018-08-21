FROM python:3.6.4-alpine

MAINTAINER Andy Driver <andy.driver@digital.justice.gov.uk>

WORKDIR /home/unidler

ADD requirements.txt requirements.txt
RUN pip install -r requirements.txt

ADD unidler.py unidler.py
ADD please_wait.html please_wait.html
ADD throbber.gif throbber.gif

CMD ["python", "unidler.py"]
