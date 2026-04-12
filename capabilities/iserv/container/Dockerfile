FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY capability.proto .
RUN python -m grpc_tools.protoc \
    -I. \
    --python_out=. \
    --grpc_python_out=. \
    capability.proto

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    rm -rf /root/.cache
RUN groupadd -g 1000 selu && useradd -u 1000 -g selu -s /bin/false selu
COPY --from=builder /build/capability_pb2.py /build/capability_pb2_grpc.py ./
COPY server.py iserv_client.py ./
USER selu
EXPOSE 50051
CMD ["python", "server.py"]
