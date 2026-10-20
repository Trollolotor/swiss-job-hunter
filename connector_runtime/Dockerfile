FROM python:3.11-slim AS python
RUN pip install --no-cache-dir beautifulsoup4==4.14.3 lxml==6.0.2 \
    && useradd --uid 10001 --create-home runner
WORKDIR /runtime
COPY connector_runtime/runner.py ./
COPY connector_runtime/connector_sdk.py /usr/local/lib/python3.11/site-packages/connector_sdk.py
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
ENTRYPOINT ["python", "-I", "/runtime/runner.py"]

FROM python AS browser
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir playwright==1.60.0 \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright
COPY connector_runtime/browser.py ./
USER 10001:10001
ENTRYPOINT ["python", "-I", "/runtime/browser.py"]
