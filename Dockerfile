FROM python:3.13.7-alpine3.22

WORKDIR /usr/app/src

COPY . ./

RUN pip install -r requirements.txt

CMD [ "python", "./app/app.py"]
