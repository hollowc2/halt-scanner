FROM python:3.12-slim
WORKDIR /app
COPY app.py .
RUN useradd --create-home scanner && mkdir /data && chown scanner:scanner /data
USER scanner
EXPOSE 8010
CMD ["python", "app.py"]
