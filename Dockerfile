# Plain -alpine on purpose. Pinned to -alpine3.22 this was frozen at Python
# 3.14.5: 3.14.6 moved to alpine3.23, no 3.14.6-alpine3.22 was ever published, and
# Renovate treats the suffix as a compatibility constraint it will not cross. The
# deployment pins a digest in the homelab repo, so reproducibility lives there.
FROM python:3.14.7-alpine

WORKDIR /usr/app/src

COPY . ./

RUN pip install -r requirements.txt

CMD [ "python", "./app/app.py"]
