# The alpine line is pinned on purpose, and Renovate moves it. A suffix after the
# version is normally a compatibility constraint Renovate will not cross, which froze
# this at Python 3.14.5 once: pinned to -alpine3.22, 3.14.6 only existed for
# alpine3.23. .github/renovate.json therefore versions this line with a regex that
# reads the alpine minor as a number, so python and alpine move together to the newest
# tag that exists. tests/test_python_version.py fails if the two stop matching. The
# deployment pins a digest in the homelab repo, so reproducibility lives there.
FROM python:3.15.2-alpine3.24

WORKDIR /usr/app/src

COPY . ./

RUN pip install -r requirements.txt

CMD [ "python", "./app/app.py"]
