FROM python:3.13.3-alpine3.21

WORKDIR /usr/app/src

COPY . ./

RUN pip install -r requirements.txt

CMD [ "python", "./app/app.py"]
