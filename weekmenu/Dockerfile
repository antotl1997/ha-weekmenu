ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN pip install --no-cache-dir "httpx>=0.27,<1" "aiohttp>=3.9,<4" "pyyaml>=6,<7"

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /
CMD ["/run.sh"]
