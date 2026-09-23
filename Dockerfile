FROM docker.io/library/python:3.14-slim-trixie AS build
# python-ldap ships no wheels: compile it against the OpenLDAP client library.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libldap2-dev libsasl2-dev \
    && rm -rf /var/lib/apt/lists/*
ARG REQUIREMENTS=requirements.txt
COPY requirements*.txt /tmp/
RUN pip wheel --no-cache-dir --require-hashes -w /wheels -r /tmp/${REQUIREMENTS}

FROM docker.io/library/python:3.14-slim-trixie
RUN apt-get update \
    && apt-get install -y --no-install-recommends libldap2 libsasl2-2 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 1000 --home /app memberbase
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index /wheels/* && rm -rf /wheels
WORKDIR /app
COPY . .
ARG GIT_COMMIT=dev
ENV GIT_COMMIT=${GIT_COMMIT}
USER memberbase
EXPOSE 5000
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "memberbase:create_app()"]
