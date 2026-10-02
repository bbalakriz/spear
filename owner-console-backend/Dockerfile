FROM registry.access.redhat.com/ubi9/python-312:latest

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 8443

CMD ["python3", "/app/server.py"]
