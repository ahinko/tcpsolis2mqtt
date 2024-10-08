FROM python:3.13.0-alpine3.20

WORKDIR /usr/app/src

COPY . ./

RUN pip install -r requirements.txt

CMD [ "python", "./app/app.py"]