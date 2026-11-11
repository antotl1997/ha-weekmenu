ARG BUILD_FROM
FROM ${BUILD_FROM}

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY app /app
COPY GIDS.md /GIDS.md
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /
CMD ["/run.sh"]
