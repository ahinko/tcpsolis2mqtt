FROM python:3.12.5-alpine3.19

WORKDIR /usr/app/src

COPY . ./

RUN pip install -r requirements.txt

CMD [ "python", "./app/app.py"]