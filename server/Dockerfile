FROM eclipse-temurin:17-jre

ARG PLC4X_VERSION=0.13.1
ENV PLC4X_VERSION=${PLC4X_VERSION}

WORKDIR /app

# Download the official uber-jar from Maven Central
# curl kept installed for runtime version updates via admin panel
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -fSL -o plc4x-opcua-server.jar \
    "https://repo1.maven.org/maven2/org/apache/plc4x/plc4x-opcua-server/${PLC4X_VERSION}/plc4x-opcua-server-${PLC4X_VERSION}.jar" && \
    rm -rf /var/lib/apt/lists/* && \
    echo "${PLC4X_VERSION}" > /app/version.txt

RUN mkdir -p /app/security

COPY entrypoint.sh logback.xml /app/
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Add Python for pylogix service
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip && rm -rf /var/lib/apt/lists/*
COPY plctag_requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r plctag_requirements.txt
COPY plctag_*.py .

RUN addgroup --system plc4x && adduser --system --ingroup plc4x plc4x
RUN chown -R plc4x:plc4x /app /app/security
USER plc4x

EXPOSE 12687
EXPOSE 5000

ENTRYPOINT ["/app/entrypoint.sh"]
