FROM python:3.11-alpine

RUN mkdir -p /usr/src/app
WORKDIR /usr/src/app

ADD requirements.txt .
ADD requirements-dev.txt .
ADD afval.py .

RUN pip install -r requirements.txt

EXPOSE 5001

ENTRYPOINT ["uvicorn"]
CMD ["afval:app", "--host", "0.0.0.0", "--port", "5000"]
